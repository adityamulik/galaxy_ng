"""Utility functions for AH tests."""

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
import yaml

from contextlib import contextmanager

from ansible.galaxy.api import GalaxyError
from orionutils.generator import build_collection as _build_collection
from orionutils.generator import randstr

from galaxy_ng.tests.integration.constants import USERNAME_PUBLISHER
from galaxy_ng.tests.integration.constants import SLEEP_SECONDS_ONETIME

from .tasks import wait_for_task
from .urls import wait_for_url


logger = logging.getLogger(__name__)


class ArtifactFile:
    def __init__(self, fn):
        self.filename = fn


def build_collection(
    base=None,
    config=None,
    filename=None,
    key=None,
    pre_build=None,
    extra_files=None,
    namespace=None,
    name=None,
    tags=None,
    version=None,
    dependencies=None,
    use_orionutils=True
):

    if base is None:
        base = "skeleton"

    if config is None:
        config = {
            "namespace": None,
            "name": None,
            "tags": []
        }

    if namespace is not None:
        config['namespace'] = namespace
    else:
        config['namespace'] = USERNAME_PUBLISHER

    if name is not None:
        config['name'] = name
    else:
        config['name'] = randstr()

    if version is not None:
        config['version'] = version

    if dependencies is not None:
        config['dependencies'] = dependencies

    if tags is not None:
        config['tags'] = tags

    # workaround for cloud importer config
    if 'tools' not in config['tags']:
        config['tags'].append('tools')

    if use_orionutils:
        return _build_collection(
            base,
            config=config,
            filename=filename,
            key=key,
            pre_build=pre_build,
            extra_files=extra_files
        )

    # use galaxy cli to build it ...
    dstdir = tempfile.mkdtemp(prefix='collection-artifact-')
    dst = None
    with tempfile.TemporaryDirectory(prefix='collection-build-') as tdir:

        cmd = f"ansible-galaxy collection init {namespace}.{name}"
        pid = subprocess.run(cmd, shell=True, cwd=tdir, stdout=subprocess.PIPE)
        assert pid.returncode == 0
        basedir = os.path.join(tdir, namespace, name)
        rolesdir = os.path.join(basedir, 'roles')

        # make a role
        cmd = "ansible-galaxy role init docker_role"
        pid2 = subprocess.run(cmd, shell=True, cwd=rolesdir, stdout=subprocess.PIPE)
        assert pid2.returncode == 0

        # fix galaxy.yml
        galaxy_file = os.path.join(basedir, 'galaxy.yml')
        with open(galaxy_file, 'r') as f:
            meta = yaml.safe_load(f.read())
        meta.update(config)
        with open(galaxy_file, 'w') as f:
            f.write(yaml.dump(meta))

        # need the meta/runtime.yml file ...
        meta_dir = os.path.join(basedir, 'meta')
        if not os.path.exists(meta_dir):
            os.makedirs(meta_dir)
        runtime_file = os.path.join(meta_dir, 'runtime.yml')
        if not os.path.exists(runtime_file):
            with open(runtime_file, 'w') as f:
                f.write(yaml.dump({'requires_ansible': '>=2.13.0'}))

        # build it
        cmd = "ansible-galaxy collection build ."
        pid3 = subprocess.run(
            cmd,
            shell=True,
            cwd=basedir,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE
        )
        assert pid3.returncode == 0
        fn = pid3.stdout.decode('utf-8').strip().split('\n')[-1].split()[-1]

        # Copy to permanent location
        dst = os.path.join(dstdir, os.path.basename(fn))
        shutil.copy(fn, dst)

    return ArtifactFile(dst)


def upload_artifact(
    config, client, artifact, hash=True, no_filename=False, no_file=False, use_distribution=True
):
    """
    Publishes a collection to a Galaxy server and returns the import task URI.

    :param collection_path: The path to the collection tarball to publish.
    :return: The import task URI that contains the import results.
    """
    collection_path = artifact.filename
    with open(collection_path, "rb") as collection_tar:
        data = collection_tar.read()

    def to_bytes(s, errors=None):
        return s.encode("utf8")

    boundary = "--------------------------%s" % uuid.uuid4().hex
    file_name = os.path.basename(collection_path)
    part_boundary = b"--" + to_bytes(boundary, errors="surrogate_or_strict")

    from ansible.galaxy.api import _urljoin
    from ansible.utils.hashing import secure_hash_s

    form = []

    if hash:
        if isinstance(hash, bytes):
            b_hash = hash
        else:
            from hashlib import sha256

            b_hash = to_bytes(secure_hash_s(data, sha256), errors="surrogate_or_strict")
        form.extend([part_boundary, b'Content-Disposition: form-data; name="sha256"', b_hash])

    if not no_file:
        if no_filename:
            form.extend(
                [
                    part_boundary,
                    b'Content-Disposition: file; name="file"',
                    b"Content-Type: application/octet-stream",
                ]
            )
        else:
            form.extend(
                [
                    part_boundary,
                    b'Content-Disposition: file; name="file"; filename="%s"' % to_bytes(file_name),
                    b"Content-Type: application/octet-stream",
                ]
            )
    else:
        form.append(part_boundary)

    form.extend([b"", data, b"%s--" % part_boundary])

    data = b"\r\n".join(form)

    headers = {
        "Content-type": "multipart/form-data; boundary=%s" % boundary,
        "Content-length": len(data),
    }

    n_url = ""
    if use_distribution:
        n_url = (
            _urljoin(
                config["url"],
                "content",
                f"inbound-{artifact.namespace}",
                "v3",
                "artifacts",
                "collections",
            )
            + "/"
        )
    else:
        n_url = _urljoin(config["url"], "v3", "artifacts", "collections") + "/"

    resp = client(n_url, args=data, headers=headers, method="POST", auth_required=True)

    return resp


@contextmanager
def modify_artifact(artifact):
    filename = artifact.filename
    with tempfile.TemporaryDirectory() as dirpath:
        # unpack
        tf = tarfile.open(filename)
        tf.extractall(dirpath)

        try:
            yield dirpath

        finally:
            # re-pack
            tf = tarfile.open(filename, "w:gz")
            for name in os.listdir(dirpath):
                tf.add(os.path.join(dirpath, name), name)
            tf.close()


def get_collections_namespace_path(namespace):
    """Get collections namespace path."""
    return os.path.expanduser(f"~/.ansible/collections/ansible_collections/{namespace}/")


def get_collection_full_path(namespace, collection_name):
    """Get collections full path."""
    return os.path.join(get_collections_namespace_path(namespace), collection_name)


def set_certification(client, collection, level="published"):
    """Moves a collection from the `staging` to the `published` repository.

    For use in instances that use repository-based certification and that
    do not have auto-certification enabled.
    """
    if client.config["use_move_endpoint"]:
        url = (
            f"v3/collections/{collection.namespace}/{collection.name}/versions/"
            f"{collection.version}/move/staging/{level}/"
        )
        job_tasks = client(url, method="POST", args=b"{}")
        assert 'copy_task_id' in job_tasks
        assert 'curate_all_synclist_repository_task_id' in job_tasks
        assert 'remove_task_id' in job_tasks

        # wait for each unique task to finish ...
        for key in ['copy_task_id', 'remove_task_id']:
            task_id = job_tasks.get(key)

            # curate is null sometimes? ...
            if task_id is None:
                continue

            # The task_id is not a url, so it has to be assembled from known data ...
            # http://.../api/automation-hub/pulp/api/v3/tasks/8be0b9b6-71d6-4214-8427-2ecf81818ed4/
            ds = {
                'task': f"{client.config['url']}/pulp/api/v3/tasks/{task_id}"
            }
            task_result = wait_for_task(client, ds)
            assert task_result['state'] == 'completed', task_result

        # callers expect response as part of this method, ensure artifact is there
        dest_url = (
            f"v3/plugin/ansible/content/{level}/collections/index/"
            f"{collection.namespace}/{collection.name}/versions/{collection.version}/"
        )
        return wait_for_url(client, dest_url)


def copy_collection_version(client, collection, src_repo_name, dest_repo_name):
    """Copies a collection from the `src_repo` to the src_repo to dest_repo."""
    url = (
        f"v3/collections/{collection.namespace}/{collection.name}/versions/"
        f"{collection.version}/copy/{src_repo_name}/{dest_repo_name}/"
    )
    job_tasks = client(url, method="POST", args=b"{}")
    assert 'task_id' in job_tasks

    # await task completion

    task_id = job_tasks.get("task_id")

    # The task_id is not a url, so it has to be assembled from known data ...
    # http://.../api/automation-hub/pulp/api/v3/tasks/8be0b9b6-71d6-4214-8427-2ecf81818ed4/
    ds = {
        'task': f"{client.config['url']}/pulp/api/v3/tasks/{task_id}"
    }
    task_result = wait_for_task(client, ds)
    assert task_result['state'] == 'completed', task_result

    # callers expect response as part of this method, ensure artifact is there
    dest_url = (
        f"v3/plugin/ansible/content/{dest_repo_name}/collections/index/"
        f"{collection.namespace}/{collection.name}/versions/{collection.version}/"
    )
    return wait_for_url(client, dest_url)


def get_all_collections_by_repo(api_client=None):
    """ Return a dict of each repo and their collections """
    assert api_client is not None, "api_client is a required param"
    api_prefix = api_client.config.get("api_prefix").rstrip("/")
    collections = {
        'staging': {},
        'published': {},
        'community': {},
    }
    for repo in collections.keys():
        next_page = f'{api_prefix}/_ui/v1/collection-versions/?repository={repo}'
        while next_page:
            resp = api_client(next_page)
            for _collection in resp['data']:
                key = (
                    _collection['namespace'],
                    _collection['name'],
                    _collection['version']
                )
                collections[repo][key] = _collection
            next_page = resp.get('links', {}).get('next')
    return collections


def get_all_repository_collection_versions(api_client):
    """ Return a dict of each repo and their collection versions """

    assert api_client is not None, "api_client is a required param"
    api_prefix = api_client.config.get("api_prefix").rstrip("/")

    repositories = [
        'staging',
        'published',
        # 'verified',
        # 'community'
    ]

    collections = []
    for repo in repositories:
        next_page = f'{api_prefix}/content/{repo}/v3/collections/'
        while next_page:
            resp = api_client(next_page)
            collections.extend(resp['data'])
            next_page = resp.get('links', {}).get('next')

    collection_versions = []
    for collection in collections:
        next_page = collection['versions_url']
        while next_page:
            resp = api_client(next_page)
            for cv in resp['data']:
                cv['namespace'] = collection['namespace']
                cv['name'] = collection['name']
                if 'staging' in cv['href']:
                    cv['repository'] = 'staging'
                elif 'published' in cv['href']:
                    cv['repository'] = 'published'
                elif 'verified' in cv['href']:
                    cv['repository'] = 'verified'
                elif 'community' in cv['href']:
                    cv['repository'] = 'community'
                else:
                    cv['repository'] = None
                collection_versions.append(cv)
            next_page = resp.get('links', {}).get('next')

    rcv = dict(
        (
            (x['repository'], x['namespace'], x['name'], x['version']),
            x
        )
        for x in collection_versions
    )
    return rcv


def delete_all_collections_in_namespace(api_client, namespace_name):

    assert api_client is not None, "api_client is a required param"
    api_prefix = api_client.config.get("api_prefix").rstrip("/")

    # accumlate a list of matching collections in each repo
    ctuples = set()
    cmap = get_all_collections_by_repo(api_client)
    for repo, cvs in cmap.items():
        for cv_spec in cvs.keys():
            if cv_spec[0] == namespace_name:
                ctuples.add((repo, cv_spec[0], cv_spec[1]))

    # delete each collection ...
    for ctuple in ctuples:

        crepo = ctuple[0]
        cname = ctuple[2]

        # Try deleting the whole collection ...
        resp = api_client(
            (f'{api_prefix}/v3/plugin/ansible/content'
             f'/{crepo}/collections/index/{namespace_name}/{cname}/'),
            method='DELETE'
        )

        # wait for the orphan_cleanup job to finish ...
        try:
            wait_for_task(api_client, resp, timeout=10000)
        except GalaxyError as ge:
            # FIXME - pulp tasks do not seem to accept token auth
            if ge.http_code in [403, 404]:
                time.sleep(SLEEP_SECONDS_ONETIME)
            else:
                raise Exception(ge)
