# Native-Agent Collaboration MVP

This workspace contains a deterministic, local-only controller candidate and its design/implementation records.

## Verify

Run from `controller-candidate`:

```powershell
python -m unittest discover -s tests -v
```

The deterministic suite does not launch native agents, messaging services, provider calls, or a real-project pilot.

See `controller-candidate/README.md` for candidate limitations and validation gates.
