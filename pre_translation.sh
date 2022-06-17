# Change Directory to clones
cd _clones

# Build Hub Development Environment
cp .compose.env.example .compose.env
./compose build

# Extract Strings from API
./compose run --rm --user=0 \
 -v $(pwd)/_translations:/translations \
 api \
 bash -c "cd /app/galaxy_ng/ && django-admin makemessages -l en_us --keep-pot"
 
cd ..

# Move files to Translations folder
mv app/galaxy_ng/locale/django.pot translations/
