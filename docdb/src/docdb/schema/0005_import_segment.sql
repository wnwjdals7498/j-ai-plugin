CREATE TABLE import_segment (
  seg_id INTEGER PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES artifact(artifact_id),
  seq INTEGER NOT NULL,
  heading TEXT,
  start_off INTEGER NOT NULL,
  end_off INTEGER NOT NULL,
  text_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','ruled','labeled','skipped','applied','stale')),
  kind_id TEXT,
  field_key TEXT,
  doc_id TEXT REFERENCES document(doc_id),
  rule TEXT,
  why TEXT,
  UNIQUE (artifact_id, seq),
  FOREIGN KEY (kind_id, field_key) REFERENCES cat_kind_field(kind_id, field_key)
);
