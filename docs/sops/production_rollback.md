# Production Deployment Rollback

Use this runbook to roll back a production deployment, release, or feature rollout.

## Required steps

1. Confirm the incident symptoms and identify the affected production service and deployment.
2. Capture the current release version, error rate, latency, and customer-impact baseline.
3. Identify and validate the last known-good release and confirm rollback compatibility.
4. Obtain explicit approval from the Incident Commander and record the approver.
5. Execute the rollback using the approved deployment mechanism.
6. Verify service health, error rate, latency, and customer recovery against the baseline.
7. Record the outcome in the incident timeline and communicate the result to responders.
