# isenfors-publish

Upload relay for isenfors.se. A bundle of changed, pre-built public files is pushed to `bundles/<id>/`;
the `publish-onecom` workflow checks it and uploads it over SFTP, then writes `results/<id>.json`.
Credentials live only in GitHub Actions secrets. `probe.yml` is a one-time connectivity test with no secrets.
