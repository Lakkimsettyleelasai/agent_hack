# Network Infrastructure Interface Manual

## Interface Diagnostics with IP Utility
When auditing active local network adapters and validating structural routing protocols, use the following operational parameters:

### Full Interface Review
```bash
ip link show
```

* Shows the exact status of all network interfaces currently attached to the system.

### Route Identification

```bash
ip route show
```

* Displays the active kernel routing tables to verify network gateways.
