# Extra CA certificates

Some portals serve an **incomplete certificate chain**: the server sends its own certificate but not
the intermediate CA that signed it. Browsers repair this silently (they download the intermediate);
curl-based clients can't, and fail with "unable to get local issuer certificate". We keep TLS
verification **on** and supply the missing public intermediate instead of disabling verification.
A source opts in with `extra_ca_certs: certs/<file>.pem` in `sources.yaml`.

| file | needed by | what it is |
|---|---|---|
| `sectigo-ov-r36.pem` | tender.nprocure.com and smctender.nprocure.com (`*.nprocure.com`) | Sectigo Public Server Authentication CA OV R36, signed by "Sectigo Public Server Authentication Root R46" (already trusted via `certifi`) |

Provenance of `sectigo-ov-r36.pem`: downloaded on 2026-10-12 from the "CA Issuers" URL in the server's own
certificate (`http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt`, 1,616 bytes), converted DER to PEM,
and checked with `openssl verify -CAfile <certifi bundle> -untrusted sectigo-ov-r36.pem <server leaf>` → OK.
SHA-256 fingerprint: `65:42:D1:76:BE:D5:0F:19:3C:0C:E2:97:AE:44:EC:D8:A0:A8:6B:EC:2E:DE:68:27:69:34:40:59:B4:E7:85:30`. Valid until 2036-03-21. These are public CA certificates, not secrets.
