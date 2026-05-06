-- ============================================================
--  University History Database Schema
--  MonetDB compatible
-- ============================================================

START TRANSACTION;

-- ── Core person ───────────────────────────────────────────────────────────────

CREATE TABLE person (
    person_id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name              VARCHAR(255),
    last_name               VARCHAR(255),
    affix                   VARCHAR(255),
    alternative_last_name   VARCHAR(255),
    gender                  VARCHAR(50),         -- 'Man' | 'Vrouw' | NULL
    faculty                 VARCHAR(255),        -- e.g. 'Theologie', 'Medicijnen'
    type_of_person          INT,                 -- 1 = professor, 2 = relative, etc.
    birth_date_begin        DATE,                -- normalised from BirthDate
    birth_date_end          DATE,
    birth_city              VARCHAR(255),
    birth_country           VARCHAR(255),
    death_date_begin        DATE,
    death_date_end          DATE,
    death_city              VARCHAR(255),
    death_country           VARCHAR(255),
    wikidata_qid            VARCHAR(20),         -- e.g. 'Q123456'
    source_file             VARCHAR(255)         -- original JSON filename
);

-- ── Location ──────────────────────────────────────────────────────────────────

CREATE TABLE location (
    location_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country         VARCHAR(255),
    city            VARCHAR(255),
    latitude        DOUBLE,
    longitude       DOUBLE,
    UNIQUE (country, city)                       -- prevent duplicate locations
);

-- ── Event types ───────────────────────────────────────────────────────────────

CREATE TABLE event_type (
    event_type_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type_name VARCHAR(100) NOT NULL,
    UNIQUE (event_type_name)
);

-- ── Events ────────────────────────────────────────────────────────────────────

CREATE TABLE event (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id       BIGINT NOT NULL,
    location_id     BIGINT,
    event_type_id   BIGINT NOT NULL,
    begin_date      DATE,
    end_date        DATE,
    description     VARCHAR(512),
    source_ref      VARCHAR(255),                -- original source citation
    FOREIGN KEY (person_id)      REFERENCES person(person_id),
    FOREIGN KEY (location_id)    REFERENCES location(location_id),
    FOREIGN KEY (event_type_id)  REFERENCES event_type(event_type_id)
);

-- ── Relations ─────────────────────────────────────────────────────────────────

CREATE TABLE relation_type (
    relation_type_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    relation_type_name  VARCHAR(50) NOT NULL,    -- 'spouse' | 'parent' | 'child'
                                                 -- | 'in_law' | 'grand_parent'
                                                 -- | 'far_family'
    UNIQUE (relation_type_name)
);

CREATE TABLE relation (
    relation_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id_1         BIGINT NOT NULL,         -- the professor
    person_id_2         BIGINT NOT NULL,         -- the relative
    relation_type_id    BIGINT NOT NULL,
    source_ref          VARCHAR(255),
    FOREIGN KEY (person_id_1)       REFERENCES person(person_id),
    FOREIGN KEY (person_id_2)       REFERENCES person(person_id),
    FOREIGN KEY (relation_type_id)  REFERENCES relation_type(relation_type_id)
);

-- ── Enrichment log ────────────────────────────────────────────────────────────
-- Tracks which fields were added by the enrichment pipeline and from which source.
-- Useful for auditing and the review dashboard.

CREATE TABLE enrichment_log (
    log_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id       BIGINT NOT NULL,
    source          VARCHAR(50),                 -- 'wikidata' | 'openarchives'
    field_name      VARCHAR(100),
    field_value     VARCHAR(512),
    needs_review    BOOLEAN DEFAULT FALSE,
    accepted        BOOLEAN,                     -- NULL = unreviewed
    FOREIGN KEY (person_id) REFERENCES person(person_id)
);

-- ── Seed event types and relation types ───────────────────────────────────────

INSERT INTO event_type (event_type_name)
    SELECT v FROM (VALUES ('birth'),('death'),('education'),('career')) AS t(v)
    WHERE NOT EXISTS (SELECT 1 FROM event_type WHERE event_type_name = t.v);

INSERT INTO relation_type (relation_type_name)
    SELECT v FROM (VALUES
        ('spouse'),('parent'),('child'),
        ('in_law'),('grand_parent'),('far_family')
    ) AS t(v)
    WHERE NOT EXISTS (SELECT 1 FROM relation_type WHERE relation_type_name = t.v);

COMMIT;