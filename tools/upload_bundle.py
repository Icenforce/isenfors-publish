#!/usr/bin/env python3
"""Runner-side uploader. Mirrors /workspace/kubuntu-publish/20261127/upload.py.
Usage: upload_bundle.py bundles/<id> results/<id>.json
Creds come from env (GitHub Actions secrets). Never prints the password.
Writes a result JSON in every case; exits non-zero on ANY problem (no silent PASS)."""
import sys, os, io, re, json, time, base64, hashlib, tarfile, posixpath

bdir, out = sys.argv[1], sys.argv[2]
res = {"status": "FAILED", "bundle": os.path.basename(bdir), "run_id": os.environ.get("GITHUB_RUN_ID"),
       "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "uploaded": [], "reason": None}
FORBIDDEN = re.compile(r"^(games/|p-[A-Za-z0-9_-]{8,}/|/)|(^|/)\.\.(/|$)")

def finish(code, reason=None):
    res["reason"] = reason; res["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if code == 0: res["status"] = "UPLOADED" if not res.get("check_only") else "CHECK_OK"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    json.dump(res, open(out, "w"), indent=1); print("RESULT", res["status"], reason or ""); sys.exit(code)

CHECK = os.environ.get("CHECK_ONLY", "").strip() == "true"
res["check_only"] = CHECK
want, data = {}, {}
if not CHECK:
  try:
      meta = json.load(open(os.path.join(bdir, "bundle.json")))
      blob = base64.b64decode("".join(open(os.path.join(bdir, c)).read() for c in meta["chunks"]))
      if hashlib.sha256(blob).hexdigest() != meta["tgz_sha256"]: finish(1, "bundle checksum mismatch")
      want = {f["path"]: f for f in meta["files"]}
      bad = [p for p in want if FORBIDDEN.search(p)]
      if bad: finish(1, "forbidden paths in bundle: %d" % len(bad))
      data = {}
      with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
          for m in tf.getmembers():
              if not m.isfile(): continue
              if m.name not in want: finish(1, "unexpected file in tgz: " + m.name)
              data[m.name] = tf.extractfile(m).read()
      for p, f in want.items():
          if p not in data: finish(1, "missing from tgz: " + p)
          if hashlib.sha256(data[p]).hexdigest() != f["sha256"] or len(data[p]) != f["size"]: finish(1, "file checksum mismatch: " + p)
      print("bundle OK:", len(want), "files")
  except SystemExit: raise
  except Exception as e: finish(1, "bundle read error: %s" % type(e).__name__)

import paramiko, urllib.request
env = lambda k: os.environ.get(k, "").strip()
host, user, pw, root = env("ONECOM_SFTP_HOST"), env("ONECOM_SFTP_USER"), env("ONECOM_SFTP_PASSWORD"), env("ONECOM_SFTP_ROOT").rstrip("/")
missing = [k for k, v in (("ONECOM_SFTP_HOST", host), ("ONECOM_SFTP_USER", user), ("ONECOM_SFTP_PASSWORD", pw)) if not v]
res["root_secret_set"] = bool(root); print("secrets present: HOST=%s USER=%s PASSWORD=%s ROOT=%s" % tuple(bool(x) for x in (host, user, pw, root)))
if missing: finish(1, "missing secret(s): " + ",".join(missing))
def live_sha():
    with urllib.request.urlopen("https://isenfors.se/?nocache=%d" % time.time(), timeout=20) as r: return hashlib.sha256(r.read()).hexdigest()
def remote_sha(s, p):
    with s.open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()
try:
    t = paramiko.Transport((host, 22)); t.banner_timeout = 20; t.start_client(timeout=20)
    k = t.get_remote_server_key(); fp = hashlib.sha256(k.asbytes()).hexdigest()
    pin = env("ONECOM_HOSTKEY_SHA256")
    if not pin: finish(1, "host key pin (repo variable ONECOM_HOSTKEY_SHA256) not set")
    if not fp.startswith(pin.lower()): finish(1, "HOST KEY MISMATCH")
    res["hostkey_pinned"] = True; print("host key matches pin")
    try: t.auth_password(user, pw)
    except paramiko.AuthenticationException: finish(1, "LOGIN FAILED (user/password rejected)")
    res["login_ok"] = True; print("login OK")
    s = paramiko.SFTPClient.from_transport(t)
    lsha = live_sha()
    def is_root(d):
        try: s.stat(d + "/ai-futures/index.html"); return remote_sha(s, d + "/index.html") == lsha
        except IOError: return False
    if root:
        if not is_root(root): finish(1, "ROOT secret set but its index.html does not match https://isenfors.se/")
    else:
        import stat as _st
        def subdirs(d):
            out = []
            try: entries = s.listdir_attr(d)
            except IOError: return out
            for e in entries:
                p = d.rstrip("/") + "/" + e.filename
                mode = e.st_mode
                if mode is not None and _st.S_ISLNK(mode):
                    try: mode = s.stat(p).st_mode          # follow symlinks
                    except IOError: continue
                if mode is not None and _st.S_ISDIR(mode): out.append(p)
            return out
        home = s.normalize("."); cands = [home]
        for c in subdirs(home): cands.append(c); cands += subdirs(c)
        print("root auto-detect: %d candidate folders (depth <= 2)" % len(cands))
        found = sorted({s.normalize(c) for c in cands if is_root(c)})
        if len(found) != 1: finish(1, "site root auto-detect found %d candidates" % len(found))
        root = found[0]; res["root_autodetected"] = True; print("site root auto-detected")
    res["root_confirmed"] = True; print("site root confirmed: index.html identical to live https://isenfors.se/")
    if CHECK: t.close(); finish(0)
    order = sorted(want, key=lambda p: p == ".htaccess")          # .htaccess last
    made = set()
    def mkd(d):
        if d in made or len(d) <= len(root): return
        mkd(posixpath.dirname(d))
        try: s.stat(d)
        except IOError: s.mkdir(d)
        made.add(d)
    for rel in order:
        rem = root + "/" + rel; mkd(posixpath.dirname(rem))
        s.putfo(io.BytesIO(data[rel]), rem + ".tmp-up")
        try: s.remove(rem)
        except IOError: pass
        s.rename(rem + ".tmp-up", rem)
        if s.stat(rem).st_size != want[rel]["size"]: finish(1, "remote size mismatch: " + rel)
        res["uploaded"].append({"path": rel, "sha256": want[rel]["sha256"]}); print("uploaded", rel)
    t.close()
except SystemExit: raise
except Exception as e: finish(1, "sftp error: %s" % type(e).__name__)
finish(0)
