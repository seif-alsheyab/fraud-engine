# Security

## Reporting

This repository is a portfolio project and is not deployed anywhere. If you
believe you have found a security issue in the code, please email
alsheyab.seif@gmail.com rather than opening a public issue.

## Known non-goals

This project deliberately does **not** implement:

- authentication or authorisation on the API
- PCI-DSS scope controls, key management, or network segmentation
- rate limiting or abuse protection on the decision endpoint

These are listed in the README under *Not included*. They are absent by
choice, not by oversight, and the code should not be deployed as-is.

## What the code does protect

- Card numbers are never stored: only a salted SHA-256 hash, plus the BIN and
  last four digits.
- `ENTITY_HASH_SALT` is never committed; CI fails if it appears in a tracked
  file.
- Card numbers, emails, IP addresses and device fingerprints are scrubbed from
  every log line, and CI fails the build if a PAN appears in the server log.
- Rule conditions are interpreted, never `eval`'d.
