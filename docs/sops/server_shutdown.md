# Production Service Change or Shutdown

Use this runbook when restarting a production service, changing production
configuration, scaling infrastructure, or shutting down a server or system.

## Required steps

1. Confirm the target system, proposed change scope, dependencies, and expected customer impact.
2. Capture current configuration, capacity, health metrics, and a reversible recovery point.
3. Validate that traffic can be drained, failed over, or restored safely if the change fails.
4. Obtain explicit approval from the Incident Commander and record the approver.
5. Execute only the approved restart, configuration change, scaling operation, or shutdown.
6. Preserve logs and verify service health, capacity, failover, and customer recovery.
7. Record the result and final metrics in the incident timeline.
