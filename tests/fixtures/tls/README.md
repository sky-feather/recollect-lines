# TLS test fixtures

`self_signed_cert.pem` and `self_signed_key.pem` are throwaway fixtures
generated solely to test local TLS certificate-verification failures (see
`tests/test_direct_api_runtime.py`). They are not production, service,
signing, SSH, or user credentials, and they are non-secret / public by
design. Do not reuse them outside tests.
