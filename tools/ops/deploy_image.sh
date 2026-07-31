#!/usr/bin/env bash
# Durable deployment: put CourseMate inside the openedx image.
#
# The dev loop (deploy_platform.sh) copies files into running containers, which
# is fine for iterating and useless for anything that starts a NEW container —
# the beat scheduler among them. Only a real install inside the image gives the
# `cms.djangoapp` entry point that puts the app in INSTALLED_APPS.
set -eu

ROOT=$(tutor config printroot)
BUILD="$ROOT/env/build/openedx"
test -d "$BUILD" || { echo "FATAL: tutor build context missing at $BUILD" >&2; exit 1; }

# The plugin's Dockerfile patch COPYs ./coursemate, so the source has to be in
# the build context — Docker cannot read outside it.
rsync -a --delete --exclude '__pycache__' --exclude '*.egg-info' \
      "$HOME/cm-build/packages" "$BUILD/coursemate/"

cp "$HOME/cm-build/deploy/tutor-plugin/coursemate.yml" "$(tutor plugins printroot)/"
tutor config save >/dev/null
echo "plugin + source staged; building"
tutor images build openedx
