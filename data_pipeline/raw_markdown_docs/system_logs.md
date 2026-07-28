# Operating System Log Diagnostics

## Service Auditing via Journalctl
To review background system daemons and verify operational integrity:

```bash
journalctl -u ssh.service -n 50 --no-pager
```

* `-u`: Restricts the diagnostic view to the explicit service name specified.
* `-n 50`: Pulls only the 50 most recent runtime logs from the buffer.
* `--no-pager`: Outputs the content directly to the standard terminal shell without pagination.
