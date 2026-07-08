#!/usr/bin/env python3
# test_digest.py — known-answer check (RFC 2617 example) for the digest auth
# in airplay-play.py. Run: python3 test_digest.py — prints "digest OK" or fails.
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
  'core', Path(__file__).with_name('airplay-play.py'))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

challenge = ('Digest realm="testrealm@host.com", qop="auth,auth-int", '
             'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
             'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
hdr = core._digest_header('GET', '/dir/index.html', challenge, 'Circle Of Life',
                          username='Mufasa', cnonce='0a4f113b')
assert 'response="6629fae49393a05397450978507c4ef1"' in hdr, hdr
print('digest OK')
