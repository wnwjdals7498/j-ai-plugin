ALTER TABLE relation ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'
  CHECK (status IN ('proposed','confirmed','rejected'));
ALTER TABLE relation ADD COLUMN source TEXT NOT NULL DEFAULT 'human';
ALTER TABLE relation ADD COLUMN proposed_by TEXT REFERENCES actor(actor_id);
ALTER TABLE relation ADD COLUMN decided_by TEXT REFERENCES actor(actor_id);
ALTER TABLE relation ADD COLUMN decided_at TEXT;

CREATE TRIGGER relation_source_status BEFORE INSERT ON relation
WHEN NOT (
     (NEW.source = 'agent' AND NEW.status = 'proposed'
      AND (SELECT actor_type FROM actor WHERE actor_id = NEW.proposed_by) = 'ai')
  OR (NEW.source = 'human'
      AND (SELECT actor_type FROM actor WHERE actor_id = NEW.proposed_by) = 'human')
  OR (NEW.source LIKE 'rule:%' AND NEW.proposed_by IS NULL AND NEW.status = 'confirmed'))
BEGIN SELECT RAISE(ABORT, 'relation source/status/proposer mismatch'); END;

CREATE TRIGGER relation_decide_human_only BEFORE UPDATE OF status ON relation
WHEN NEW.status <> OLD.status
 AND (OLD.status <> 'proposed'
      OR coalesce((SELECT actor_type FROM actor WHERE actor_id = NEW.decided_by), '') <> 'human')
BEGIN SELECT RAISE(ABORT, 'only a human can decide a proposed relation'); END;

CREATE VIEW v_missing_required_links AS
WITH ends AS (
  SELECT from_type AS a_type, from_id AS a_id, to_type AS b_type, to_id AS b_id
  FROM relation WHERE status = 'confirmed'
  UNION ALL
  SELECT to_type, to_id, from_type, from_id FROM relation WHERE status = 'confirmed'),
owner AS (
  SELECT 'document' AS t, doc_id AS id, doc_id FROM document
  UNION ALL
  SELECT 'item', item_id, defined_in FROM item WHERE defined_in IS NOT NULL),
linked AS (
  SELECT oa.doc_id AS doc_id, ob.doc_id AS other_doc
  FROM ends e
  JOIN owner oa ON oa.t = e.a_type AND oa.id = e.a_id
  JOIN owner ob ON ob.t = e.b_type AND ob.id = e.b_id)
SELECT dk.doc_id, dk.kind_id, l.target_kind_id
FROM document_kind dk
JOIN document d ON d.doc_id = dk.doc_id AND d.applicability_status <> 'not_applicable'
JOIN cat_kind_link l ON l.kind_id = dk.kind_id AND l.is_required = 1
WHERE NOT EXISTS (
    SELECT 1 FROM link_exemption e
    WHERE e.doc_id = dk.doc_id AND e.target_kind_id = l.target_kind_id)
  AND NOT EXISTS (
    SELECT 1 FROM linked k
    JOIN document_kind dk2 ON dk2.doc_id = k.other_doc
    WHERE k.doc_id = dk.doc_id AND dk2.kind_id = l.target_kind_id);

DROP VIEW v_rtm;
CREATE VIEW v_rtm AS
SELECT r.item_id AS requirement_id, r.title,
       group_concat(DISTINCT CASE WHEN rel.rel_type = 'verified_by' THEN rel.to_id END) AS verified_by,
       group_concat(DISTINCT CASE WHEN rel.rel_type = 'implements' THEN rel.from_id END) AS implemented_by
FROM item r
LEFT JOIN relation rel ON (
       (rel.from_type = 'item' AND rel.from_id = r.item_id AND rel.rel_type = 'verified_by')
    OR (rel.to_type = 'item' AND rel.to_id = r.item_id AND rel.rel_type = 'implements'))
    AND rel.status = 'confirmed'
WHERE r.item_type = 'requirement'
GROUP BY r.item_id;
