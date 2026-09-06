import type {
  Action, Claim, Conflict, Decision, Fact, Hypothesis, TranscriptEvent, Unknown,
} from '../types';

// Complete frontend-only replay for product demos. This data is never POSTed.
const at = (minute: number, second = 0) =>
  new Date(Date.UTC(2026, 8, 6, 10, minute, second)).toISOString();

const transcripts: TranscriptEvent[] = [
  { id: -10, timestamp: at(0), speaker: 'Priya Nair', role: 'Incident Commander', text: 'We have a P1 checkout outage. Payment requests are returning 503s across all regions. Rahul, check the service; Meera, take infrastructure.' },
  { id: -9, timestamp: at(1), speaker: 'Rahul Shah', role: 'Backend Engineer', text: 'Payment API error rate is 38.2%. The pricing servlet is timing out while acquiring database connections.' },
  { id: -8, timestamp: at(2), speaker: 'Arjun Rao', role: 'Database Engineer', text: 'Primary database CPU is 96%, with 41 waiting connections and 186 long-running pricing queries.' },
  { id: -7, timestamp: at(3), speaker: 'Meera Iyer', role: 'DevOps/SRE', text: "The core payment service looks healthy, so I'm not sure whether this is an application failure or database saturation." },
  { id: -6, timestamp: at(3, 12), speaker: 'ResQVoice Agent', role: 'AI Incident Teammate', text: "I found the mismatch. Core payments is healthy, but the pricing servlet is failing its database health check. I'm checking the last deployment and its logs now." },
  { id: -5, timestamp: at(4), speaker: 'Sofia Martin', role: 'Customer Support Lead', text: 'We have 2,847 failed checkouts and 317 customer reports in the last 15 minutes. Impact is still rising.' },
  { id: -4, timestamp: at(5), speaker: 'ResQVoice Agent', role: 'AI Incident Teammate', text: 'I isolated the fault to the pricing servlet deployed at 09:42 UTC. I am restarting only that component in the demo sandbox and leaving the healthy payment workers untouched.' },
  { id: -3, timestamp: at(6), speaker: 'Meera Iyer', role: 'DevOps/SRE', text: 'Restart completed. New pods are healthy and the database connection queue is draining.' },
  { id: -2, timestamp: at(7), speaker: 'Rahul Shah', role: 'Backend Engineer', text: 'Payment error rate is back to 0.4%, p95 latency is 420 milliseconds, and test checkouts are succeeding.' },
  { id: -1, timestamp: at(7, 10), speaker: 'ResQVoice Agent', role: 'AI Incident Teammate', text: "Recovery is verified. I resolved the evidence gap, assigned the follow-up, and drafted the handoff. I'll keep watching the error rate while the team confirms customer recovery." },
];

export const MOCK_PAYMENT_OUTAGE = {
  facts: [
    { id: 'fact-impact', description: 'Checkout requests returned HTTP 503 responses in all production regions.', source: 'Payment API telemetry', speaker: 'Rahul Shah', timestamp: at(1), status: 'CONFIRMED' },
    { id: 'fact-errors', description: 'Payment API error rate peaked at 38.2%.', source: 'Grafana / payment-api', speaker: 'Rahul Shah', timestamp: at(1), status: 'CONFIRMED' },
    { id: 'fact-db', description: 'Primary database reached 96% CPU with 41 waiting connections and 186 long-running queries.', source: 'Database performance insights', speaker: 'Arjun Rao', timestamp: at(2), status: 'CONFIRMED' },
    { id: 'fact-deploy', description: 'Pricing servlet release v2.18.4 was deployed at 09:42 UTC, 18 minutes before impact.', source: 'Deployment audit log', speaker: 'ResQVoice Agent', timestamp: at(4), status: 'CONFIRMED' },
    { id: 'fact-recovery', description: 'After targeted restart, errors fell to 0.4%, p95 latency reached 420 ms, and synthetic checkouts passed.', source: 'Automated recovery probe', speaker: 'ResQVoice Agent', timestamp: at(7), status: 'CONFIRMED' },
  ] satisfies Fact[],
  hypotheses: [
    { id: 'hyp-pricing-pool', description: 'Pricing servlet v2.18.4 leaked database connections, exhausting the shared pool and breaking checkout.', origin: 'ResQVoice Agent', supporting_evidence: ['fact-db', 'fact-deploy', 'fact-recovery'], contradicting_evidence: [], confidence: 0.94, status: 'CONFIRMED' },
    { id: 'hyp-payment-workers', description: 'The core payment worker fleet caused the outage.', origin: 'Initial responder assumption', supporting_evidence: ['fact-errors'], contradicting_evidence: ['Core worker health checks remained green', 'Only pricing servlet logs showed pool timeouts'], confidence: 0.08, status: 'REJECTED' },
  ] satisfies Hypothesis[],
  actions: [
    { id: 'action-restart', task: 'Restart only the unhealthy pricing servlet deployment.', owner: 'Meera Iyer', status: 'COMPLETED', priority: 'P1', created_at: at(4), deadline: at(6) },
    { id: 'action-queries', task: 'Terminate orphaned pricing queries and confirm the connection queue drains.', owner: 'Arjun Rao', status: 'COMPLETED', priority: 'P1', created_at: at(4), deadline: at(7) },
    { id: 'action-verify', task: 'Run synthetic checkout probes and monitor error rate for ten minutes.', owner: 'Rahul Shah', status: 'IN_PROGRESS', priority: 'P1', created_at: at(5), deadline: at(17) },
    { id: 'action-followup', task: 'Add connection-pool saturation alerts and review v2.18.4 for a leak.', owner: 'Meera Iyer', status: 'TODO', priority: 'P2', created_at: at(7), deadline: at(37) },
  ] satisfies Action[],
  decisions: [
    { id: 'decision-targeted-restart', recommendation: 'Restart only pricing-servlet v2.18.4; do not restart the healthy payment worker fleet.', evidence: ['38.2% checkout errors', 'Pricing servlet DB pool timeout', 'Core worker health checks green'], sop_reference: 'SOP-PAY-07 §4.2 Targeted service recovery', approved_by: 'Priya Nair', approval_time: at(4, 20), execution_status: 'COMPLETED', result: 'Pricing servlet restarted in DEMO_SANDBOX; error rate recovered to 0.4%.' },
    { id: 'decision-monitor', recommendation: 'Keep the incident at P1 until ten minutes of healthy synthetic checkouts are observed.', evidence: ['Recovery probe passed', 'Customer-impact window still active'], sop_reference: 'SOP-INC-02 §6 Recovery monitoring', approved_by: 'Priya Nair', approval_time: at(7, 20), execution_status: 'APPROVED', result: 'Monitoring window active; resolution criteria recorded.' },
  ] satisfies Decision[],
  claims: [
    { id: 'claim-outage', text: 'Checkout is failing with HTTP 503 responses in every region.', speaker: 'Priya Nair', role: 'Incident Commander', timestamp: at(0), status: 'CONFIRMED', supporting: ['fact-impact', 'fact-errors'], contradicting: [] },
    { id: 'claim-timeouts', text: 'The pricing servlet is timing out while acquiring database connections.', speaker: 'Rahul Shah', role: 'Backend Engineer', timestamp: at(1), status: 'CONFIRMED', supporting: ['fact-errors', 'fact-db'], contradicting: [] },
    { id: 'claim-db-saturation', text: 'Database CPU and waiting connections indicate pool saturation.', speaker: 'Arjun Rao', role: 'Database Engineer', timestamp: at(2), status: 'CONFIRMED', supporting: ['fact-db'], contradicting: [] },
    { id: 'claim-core-healthy', text: 'The core payment worker fleet is healthy.', speaker: 'Meera Iyer', role: 'DevOps/SRE', timestamp: at(3), status: 'CONFIRMED', supporting: ['Core worker health checks remained green'], contradicting: ['claim-outage'] },
    { id: 'claim-deploy', text: 'Release v2.18.4 introduced the pricing servlet connection leak.', speaker: 'ResQVoice Agent', role: 'AI Incident Teammate', timestamp: at(4), status: 'CORROBORATED', supporting: ['fact-deploy', 'fact-recovery'], contradicting: [] },
    { id: 'claim-recovered', text: 'Checkout recovered after the targeted pricing servlet restart.', speaker: 'Rahul Shah', role: 'Backend Engineer', timestamp: at(7), status: 'CONFIRMED', supporting: ['fact-recovery'], contradicting: [] },
  ] satisfies Claim[],
  conflicts: [
    { id: 'conflict-service-health', topic: 'Payment platform health versus checkout availability', claim_a_id: 'claim-outage', claim_b_id: 'claim-core-healthy', status: 'RESOLVED', recommended_verification: 'Compare health checks by component and run a synthetic checkout through the pricing path.' },
  ] satisfies Conflict[],
  unknowns: [
    { id: 'unknown-component', description: 'Which payment component is unhealthy while core worker checks remain green?', status: 'RESOLVED', source_id: 'conflict-service-health' },
    { id: 'unknown-change', description: 'What production change preceded the first checkout failure?', status: 'RESOLVED', source_id: 'claim-deploy' },
  ] satisfies Unknown[],
  agent_runtime: {
    timestamp: at(7, 10),
    objective: 'Restore checkout safely, preserve healthy services, and keep responders aligned with verified evidence.',
    plan: [
      { step: 'Confirm customer impact and affected transaction path', status: 'DONE' },
      { step: 'Resolve contradictions and evidence gaps', status: 'DONE' },
      { step: 'Isolate the failing component and recent change', status: 'DONE' },
      { step: 'Execute the smallest safe remediation', status: 'DONE' },
      { step: 'Verify recovery metrics and assign follow-up work', status: 'DONE' },
    ],
    activity: ['Detected a P1 checkout outage from the live conversation.', 'Reconciled healthy core workers with a failing pricing path.', 'Correlated database saturation with pricing-servlet v2.18.4.', 'Restarted only the affected servlet in DEMO_SANDBOX.', 'Verified recovery and assigned remaining work.'],
    tool_call: { tool: 'check_service_health', mode: 'DEMO_SANDBOX', result: { service: 'pricing-servlet', status: 'degraded', healthy_instances: '3/8' } },
    tool_calls: [
      { tool: 'check_service_health', mode: 'DEMO_SANDBOX', result: { service: 'pricing-servlet', status: 'degraded', healthy_instances: '3/8' } },
      { tool: 'inspect_service_logs', mode: 'DEMO_SANDBOX', result: { error: 'DB_POOL_ACQUIRE_TIMEOUT', occurrences: 1247, first_seen_utc: '09:59:41' } },
      { tool: 'get_recent_deployments', mode: 'DEMO_SANDBOX', result: { release: 'v2.18.4', deployed_at_utc: '09:42', changed_component: 'pricing-servlet' } },
    ],
    diagnosis: 'Pricing servlet v2.18.4 leaked database connections, exhausting its pool while the core payment workers remained healthy.',
    remediation: { action: 'Restarted pricing-servlet only and drained orphaned database sessions.', status: 'SIMULATED_EXECUTED', mode: 'DEMO_SANDBOX' },
    recovery_check: { tool: 'get_error_rate', mode: 'DEMO_SANDBOX', result: { recovered: true, error_rate_percent: 0.4, p95_latency_ms: 420, synthetic_checkout: 'passed' } },
    recommended_owner: { name: 'Meera Iyer', role: 'DevOps/SRE' },
    hypothesis_updates: [{ hypothesis_id: 'hyp-pricing-pool', previous_confidence: 0.62, new_confidence: 0.94, evidence_overlap: ['DB pool timeout', 'v2.18.4 deploy', 'restart recovery'] }],
    next_question: "I restarted only the affected pricing servlet and verified recovery. I'll keep watching the error rate while Rahul confirms customer checkouts.",
  },
  agent_activity: [
    { id: 'activity-10', timestamp: at(7, 10), phase: 'update', title: 'Spoke recovery update to the room', detail: 'Reported the targeted action, evidence, and monitoring owner in natural language.', status: 'active' as const },
    { id: 'activity-9', timestamp: at(7, 8), phase: 'verify', title: 'Verified checkout recovery', detail: 'Error rate 0.4%, p95 latency 420 ms, synthetic checkout passed.', status: 'complete' as const },
    { id: 'activity-8', timestamp: at(7, 5), phase: 'act', title: 'Assigned recovery monitoring', detail: 'Assigned Rahul Shah to monitor synthetic checkout probes for ten minutes.', status: 'complete' as const },
    { id: 'activity-7', timestamp: at(6), phase: 'act', title: 'Executed targeted remediation', detail: 'Restarted pricing-servlet only and drained orphaned sessions in DEMO_SANDBOX.', status: 'complete' as const },
    { id: 'activity-6', timestamp: at(5), phase: 'decide', title: 'Selected smallest safe action', detail: 'Avoided a whole-platform restart because core payment workers were healthy.', status: 'complete' as const },
    { id: 'activity-5', timestamp: at(4, 30), phase: 'verify', title: 'Resolved evidence gaps', detail: 'Component health and deployment evidence reconciled both open questions.', status: 'complete' as const },
    { id: 'activity-4', timestamp: at(4), phase: 'tool', title: 'Inspected deployments and logs', detail: 'Found DB_POOL_ACQUIRE_TIMEOUT after pricing-servlet v2.18.4.', status: 'complete' as const },
    { id: 'activity-3', timestamp: at(3, 15), phase: 'update', title: 'Intervened in the conversation', detail: 'Explained the component-level mismatch and announced the next investigation.', status: 'complete' as const },
    { id: 'activity-2', timestamp: at(3), phase: 'plan', title: 'Detected contradictory evidence', detail: 'Checkout was unavailable while core payment health checks remained green.', status: 'complete' as const },
    { id: 'activity-1', timestamp: at(0), phase: 'observe', title: 'Detected P1 customer impact', detail: 'Recognized widespread HTTP 503 checkout failures from the live call.', status: 'complete' as const },
  ],
  transcripts,
};

export const MOCK_PAYMENT_OUTAGE_TRANSCRIPTS = transcripts;

export const MOCK_REPLAY_STAGES = [
  {
    label: 'Signal detected',
    title: 'A P1 checkout outage enters the room',
    narration: 'ResQVoice recognizes the severity, customer impact, and affected transaction path directly from the call.',
    cue: 'Start here: responders only describe what they can observe.',
  },
  {
    label: 'Evidence conflict',
    title: 'The evidence does not agree yet',
    narration: 'The platform is failing, but core payment health checks are green. The agent turns that contradiction into two explicit investigation questions.',
    cue: 'Point out that uncertainty is recorded instead of silently ignored.',
  },
  {
    label: 'Agent intervention',
    title: 'The agent joins the conversation',
    narration: 'After a natural pause, ResQVoice explains the component-level mismatch and announces the check it is performing next.',
    cue: 'Highlight that no responder clicked a button or issued a command.',
  },
  {
    label: 'Root cause isolated',
    title: 'Health, logs, and deployments converge',
    narration: 'Autonomous tool calls connect database pool timeouts to pricing-servlet v2.18.4 and clear the evidence gaps.',
    cue: 'Show the confidence change from 62% to 94%.',
  },
  {
    label: 'Targeted action',
    title: 'The smallest safe remediation executes',
    narration: 'The agent restarts only the unhealthy pricing servlet in the demo sandbox and preserves the healthy payment workers.',
    cue: 'Emphasize that a risky whole-platform restart was avoided.',
  },
  {
    label: 'Recovery verified',
    title: 'Evidence closes the loop',
    narration: 'Error rate falls to 0.4%, synthetic checkout passes, owners are assigned, and the agent gives the room a concise recovery update.',
    cue: 'Finish on 5/5 plan steps, zero unresolved signals, and the audit trail.',
  },
] as const;

const stageCounts = [
  { transcripts: 1, facts: 1, claims: 1, activities: 1 },
  { transcripts: 4, facts: 3, claims: 4, activities: 2 },
  { transcripts: 5, facts: 3, claims: 4, activities: 3 },
  { transcripts: 6, facts: 4, claims: 5, activities: 5 },
  { transcripts: 8, facts: 4, claims: 5, activities: 8 },
  { transcripts: 10, facts: 5, claims: 6, activities: 10 },
] as const;

export function buildMockReplayState(requestedStage: number) {
  const stage = Math.max(0, Math.min(requestedStage, MOCK_REPLAY_STAGES.length - 1));
  const counts = stageCounts[stage];
  const planDone = stage;
  const plan = MOCK_PAYMENT_OUTAGE.agent_runtime.plan.map((item, index) => ({
    ...item,
    status: index < planDone ? 'DONE' : index === planDone ? 'ACTIVE' : 'PENDING',
  }));

  const hypotheses = stage < 1
    ? []
    : MOCK_PAYMENT_OUTAGE.hypotheses.map((item, index) => ({
        ...item,
        confidence: stage >= 3 ? item.confidence : index === 0 ? 0.62 : 0.35,
        status: stage >= 3 ? item.status : 'UNCONFIRMED',
      }));
  const conflicts = stage < 1
    ? []
    : MOCK_PAYMENT_OUTAGE.conflicts.map(item => ({ ...item, status: stage >= 3 ? 'RESOLVED' : 'OPEN' }));
  const unknowns = stage < 1
    ? []
    : MOCK_PAYMENT_OUTAGE.unknowns.map(item => ({ ...item, status: stage >= 3 ? 'RESOLVED' : 'OPEN' }));
  const actions = stage < 2
    ? []
    : stage === 2
      ? [{ ...MOCK_PAYMENT_OUTAGE.actions[0], owner: 'Meera Iyer', status: 'IN_PROGRESS' }]
      : stage === 3
        ? [{ ...MOCK_PAYMENT_OUTAGE.actions[0], status: 'IN_PROGRESS' }]
        : stage === 4
          ? MOCK_PAYMENT_OUTAGE.actions.slice(0, 3)
          : MOCK_PAYMENT_OUTAGE.actions;
  const decisions = stage < 3
    ? []
    : stage < 5
      ? [{ ...MOCK_PAYMENT_OUTAGE.decisions[0], execution_status: stage === 3 ? 'APPROVED' : 'COMPLETED' }]
      : MOCK_PAYMENT_OUTAGE.decisions;
  const stageTranscript = transcripts.slice(0, counts.transcripts);
  const runtime = stage === 0 ? null : {
    ...MOCK_PAYMENT_OUTAGE.agent_runtime,
    timestamp: stageTranscript.at(-1)?.timestamp ?? at(stage),
    plan,
    tool_calls: stage < 2 ? [] : MOCK_PAYMENT_OUTAGE.agent_runtime.tool_calls.slice(0, stage >= 3 ? 3 : 1),
    diagnosis: stage >= 3 ? MOCK_PAYMENT_OUTAGE.agent_runtime.diagnosis : undefined,
    remediation: stage >= 4 ? MOCK_PAYMENT_OUTAGE.agent_runtime.remediation : undefined,
    recovery_check: stage >= 5 ? MOCK_PAYMENT_OUTAGE.agent_runtime.recovery_check : null,
    hypothesis_updates: stage >= 3 ? MOCK_PAYMENT_OUTAGE.agent_runtime.hypothesis_updates : [],
    next_question: [
      '',
      'I found conflicting health signals. I am checking which component is actually failing.',
      "Core payments is healthy, but the pricing servlet is failing its database check. I'm inspecting its logs and latest deployment now.",
      'I isolated the fault to pricing-servlet v2.18.4. I am preparing the smallest safe recovery action.',
      'I restarted only the affected pricing servlet. I am verifying checkout recovery before we stand down.',
      MOCK_PAYMENT_OUTAGE.agent_runtime.next_question,
    ][stage],
  };

  return {
    ...MOCK_PAYMENT_OUTAGE,
    facts: MOCK_PAYMENT_OUTAGE.facts.slice(0, counts.facts),
    hypotheses,
    actions,
    decisions,
    claims: MOCK_PAYMENT_OUTAGE.claims.slice(0, counts.claims),
    conflicts,
    unknowns,
    agent_runtime: runtime,
    agent_activity: MOCK_PAYMENT_OUTAGE.agent_activity.slice(-counts.activities),
    transcripts: stageTranscript,
  };
}
