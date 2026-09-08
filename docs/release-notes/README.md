# Release notes

One file per tag, named exactly after it: `v1.6.0.md` for tag `v1.6.0`.

The release workflow uses the matching file as the release body. When there
isn't one it falls back to GitHub's generated commit list, which is accurate
but reads like a changelog rather than something written for the person
downloading the exe — so write the file when the release is worth explaining.

Cutting a release:

```bash
# 1. bump both, they must agree or the build refuses
#      shorts_generator/version.py   APP_VERSION
#      version_info.txt              filevers / prodvers / File / ProductVersion
# 2. write docs/release-notes/v1.6.0.md
# 3. commit, then:
git tag -a v1.6.0 -m "Stream to Shorts v1.6.0"
git push origin main
git push origin v1.6.0
```

The workflow builds the exe on a GitHub runner and publishes the release.
Every installed copy from v1.5.0 onward will offer it to its user.
