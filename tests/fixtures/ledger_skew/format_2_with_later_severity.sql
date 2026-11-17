CREATE TABLE ledger_meta (
	"key" VARCHAR NOT NULL,
	value VARCHAR NOT NULL,
	PRIMARY KEY ("key")
);
INSERT INTO ledger_meta ("key", value) VALUES ('format', '2');
INSERT INTO ledger_meta ("key", value) VALUES ('written_by', '9.9.9');
INSERT INTO ledger_meta ("key", value) VALUES ('written_at', '2026-11-17T12:00:00+00:00');
CREATE TABLE pr_findings (
	id INTEGER NOT NULL,
	pr_number INTEGER NOT NULL,
	head_sha VARCHAR NOT NULL,
	round INTEGER NOT NULL,
	finder VARCHAR NOT NULL,
	claim_type VARCHAR NOT NULL,
	severity VARCHAR NOT NULL,
	file VARCHAR NOT NULL,
	line_start INTEGER NOT NULL,
	line_end INTEGER NOT NULL,
	claim VARCHAR NOT NULL,
	evidence_pointer VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	verification_method VARCHAR NOT NULL,
	verification_result VARCHAR NOT NULL,
	checked_at_sha VARCHAR NOT NULL,
	verify_attempts INTEGER NOT NULL,
	last_seen_at VARCHAR NOT NULL,
	last_run_id VARCHAR NOT NULL,
	budget_used INTEGER NOT NULL,
	PRIMARY KEY (id)
);
INSERT INTO pr_findings VALUES (1, 259, 'ba9585eb3c1d0f6e2a4b7c8d9e0f1a2b3c4d5e6f', 1, 'sentinel', 'broken_behavior', 'later', 'src/repoach/review/findings.py', 90, 102, 'a severity a newer writer defines', 'findings.py:90', 'proposed', '', '', '', 0, '', '', 0);
