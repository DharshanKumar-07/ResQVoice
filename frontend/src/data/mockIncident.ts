import type { TranscriptEvent } from '../types';

// Frontend-only copy of payment_outage_transcript.json. These records are
// never POSTed to the backend and cannot contaminate the live incident store.
export const MOCK_PAYMENT_OUTAGE_TRANSCRIPTS: TranscriptEvent[] = [
  {
    id: -6,
    timestamp: '2023-10-27T10:00:00Z',
    speaker: 'Priya',
    role: 'Incident Commander',
    text: "Alright everyone, we have a P1. Support is reporting that users can't checkout, they are getting 500 errors on the payment page. Rahul, what are you seeing?",
  },
  {
    id: -5,
    timestamp: '2023-10-27T10:01:00Z',
    speaker: 'Rahul',
    role: 'Backend Engineer',
    text: "I'm looking at the logs now. The payment service is throwing connection timeouts to the database. It looks like the connections are maxing out.",
  },
  {
    id: -4,
    timestamp: '2023-10-27T10:02:00Z',
    speaker: 'Arjun',
    role: 'Database Engineer',
    text: 'Let me check the primary DB instance. Yes, CPU is at 100% and there are a lot of stuck queries from the payment service.',
  },
  {
    id: -3,
    timestamp: '2023-10-27T10:03:00Z',
    speaker: 'Meera',
    role: 'DevOps/SRE',
    text: 'I think this might be related to the new pricing tier deployment we did an hour ago. Could that be causing inefficient queries?',
  },
  {
    id: -2,
    timestamp: '2023-10-27T10:04:00Z',
    speaker: 'Support',
    role: 'Support Engineer',
    text: "Just confirming, customer complaints have spiked in the last 15 minutes. It's definitely a widespread issue.",
  },
  {
    id: -1,
    timestamp: '2023-10-27T10:05:00Z',
    speaker: 'Priya',
    role: 'Incident Commander',
    text: 'Okay, Meera, please rollback the pricing tier deployment immediately as a mitigation step. Arjun, kill those stuck queries to free up the DB.',
  },
];
