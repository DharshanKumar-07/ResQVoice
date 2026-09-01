CREATE TYPE hypothesisstatus AS ENUM ('UNCONFIRMED', 'CORROBORATED', 'DISPUTED', 'CONFIRMED', 'REJECTED');
CREATE TYPE actionstatus AS ENUM ('TODO', 'IN_PROGRESS', 'BLOCKED', 'COMPLETED', 'CANCELLED', 'OVERDUE');
CREATE TABLE facts (
	id VARCHAR NOT NULL, 
	description VARCHAR NOT NULL, 
	source VARCHAR, 
	speaker VARCHAR, 
	timestamp TIMESTAMP WITHOUT TIME ZONE, 
	status VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_facts_id ON facts (id);
CREATE TABLE hypotheses (
	id VARCHAR NOT NULL, 
	description VARCHAR NOT NULL, 
	origin VARCHAR, 
	supporting_evidence VARCHAR[], 
	contradicting_evidence VARCHAR[], 
	confidence FLOAT, 
	status hypothesisstatus, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_hypotheses_id ON hypotheses (id);
CREATE TABLE claims (
	id VARCHAR NOT NULL, 
	text VARCHAR NOT NULL, 
	speaker VARCHAR, 
	role VARCHAR, 
	timestamp TIMESTAMP WITHOUT TIME ZONE, 
	status VARCHAR, 
	supporting VARCHAR[], 
	contradicting VARCHAR[], 
	PRIMARY KEY (id)
);
CREATE INDEX ix_claims_id ON claims (id);
CREATE TABLE evidence (
	id VARCHAR NOT NULL, 
	claim_id VARCHAR NOT NULL, 
	type VARCHAR, 
	description VARCHAR, 
	source VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_evidence_id ON evidence (id);
CREATE TABLE conflicts (
	id VARCHAR NOT NULL, 
	topic VARCHAR, 
	claim_a_id VARCHAR, 
	claim_b_id VARCHAR, 
	status VARCHAR, 
	recommended_verification VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_conflicts_id ON conflicts (id);
CREATE TABLE unknowns (
	id VARCHAR NOT NULL, 
	description VARCHAR, 
	status VARCHAR, 
	linked_action_id VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_unknowns_id ON unknowns (id);
CREATE TABLE actions (
	id VARCHAR NOT NULL, 
	task VARCHAR NOT NULL, 
	owner VARCHAR, 
	status actionstatus, 
	priority VARCHAR, 
	created_at TIMESTAMP WITHOUT TIME ZONE, 
	deadline TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_actions_id ON actions (id);
CREATE TABLE decisions (
	id VARCHAR NOT NULL, 
	recommendation VARCHAR, 
	evidence VARCHAR[], 
	sop_reference VARCHAR, 
	approved_by VARCHAR, 
	approval_time TIMESTAMP WITHOUT TIME ZONE, 
	execution_status VARCHAR, 
	result VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_decisions_id ON decisions (id);
CREATE TABLE timeline_events (
	id VARCHAR NOT NULL, 
	timestamp TIMESTAMP WITHOUT TIME ZONE, 
	description VARCHAR, 
	event_type VARCHAR, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_timeline_events_id ON timeline_events (id);
