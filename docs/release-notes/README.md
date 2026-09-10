# Release notes

The install instructions that ship *inside* the downloads live in
`docs/install/`: `macos.txt` goes into the zip as `READ ME FIRST.txt`, and
`linux.txt` is published beside the binary as
`StreamToShorts-linux-README.txt`. They repeat what the README says, for the
person holding the download and not the page — so a change to how either
platform is installed or used belongs in both.

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

It also builds `StreamToShorts-macOS-arm64.zip` on a macOS runner. **Say in the
notes that the mac build is a beta and has not been run on a Mac** — it is
built and checked by CI, and that is all. Someone downloading it should know
that before they do, not after.

And `StreamToShorts-linux-x86_64`, built inside an Ubuntu 22.04 container so
the glibc floor is chosen rather than inherited. **Say in the notes that it
has not been tested on a desktop distribution.** The workflow does more for
this one than for either of the others — it starts the binary, fetches the
interface out of it, and measures what glibc the unpacked payload actually
needs — but a container is not a desktop, and nobody has yet made clips with
it on one.

## Trying a build without releasing it

Run the **Release** workflow by hand from the Actions tab, tick **dry run**,
and leave the tag empty. It builds all three apps from the branch you picked,
checks them, publishes nothing, and attaches the results to the run for a
week.

Worth doing before a tag when anything about packaging changed — especially
for the mac build, which cannot be tried here first. What it proves: that it
builds, that the version and signature are right, that ffmpeg survived
bundling, and — on Linux only — that the app starts and serves its interface.
What it cannot prove: that the app opens on a real Mac, or that a real Linux
desktop behaves like the container it was built in.
