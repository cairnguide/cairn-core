-- 0002: core relational tables: users, cases, case_members, deceased, death_events.
-- Target: PostgreSQL 15 or newer.

CREATE DOMAIN cairn.state_code AS text
  CHECK (VALUE ~ '^[A-Z]{2}$');

CREATE DOMAIN cairn.tri_state AS text
  CHECK (VALUE IN ('yes', 'no', 'unknown'));

CREATE FUNCTION cairn.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

CREATE TABLE cairn.users (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  idp_subject text NOT NULL UNIQUE CHECK (length(idp_subject) BETWEEN 1 AND 255),
  email       text NOT NULL CHECK (length(email) BETWEEN 3 AND 320),
  first_name  text NOT NULL CHECK (length(first_name) BETWEEN 1 AND 100),
  last_name   text NOT NULL CHECK (length(last_name) BETWEEN 1 AND 100),
  phone       text CHECK (phone IS NULL OR length(phone) BETWEEN 7 AND 32),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_lower_uq ON cairn.users (lower(email));

CREATE TABLE cairn.cases (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  status             text NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'paused', 'closed')),
  journey_started_on date NOT NULL DEFAULT current_date,
  tasks_paused_until timestamptz,
  created_by         uuid NOT NULL REFERENCES cairn.users (id),
  created_at         timestamptz NOT NULL DEFAULT now(),
  purge_after        timestamptz
);
CREATE INDEX cases_created_by_idx ON cairn.cases (created_by);
CREATE INDEX cases_purge_after_idx ON cairn.cases (purge_after) WHERE purge_after IS NOT NULL;

CREATE TABLE cairn.case_members (
  case_id      uuid NOT NULL REFERENCES cairn.cases (id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES cairn.users (id),
  relationship text NOT NULL
                 CHECK (relationship IN ('spouse', 'child', 'sibling', 'other_family',
                                         'power_of_attorney', 'fiduciary')),
  role         text NOT NULL
                 CHECK (role IN ('owner', 'co_executor', 'fiduciary', 'attorney',
                                 'power_of_attorney', 'viewer')),
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (case_id, user_id),
  -- MVP allows a single owner per case. A later migration drops this constraint
  -- when invitations and co-executors ship. No schema change is needed otherwise.
  CONSTRAINT mvp_owner_only CHECK (role = 'owner')
);
CREATE INDEX case_members_user_idx ON cairn.case_members (user_id);

-- deceased and death_events were originally separate tables. They were merged
-- because the relationship is one-to-one and always accessed together, and the
-- merge lets the database enforce date_of_death against date_of_birth directly.
-- The application still collects these fields in two steps (identity first, then
-- the death event), which now means an INSERT followed by an UPDATE on the same
-- row rather than an insert into a second table. See UC-5 and UC-6 in
-- docs/cairn-mvp-use-cases.md.
CREATE TABLE cairn.deceased (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  case_id          uuid NOT NULL UNIQUE REFERENCES cairn.cases (id) ON DELETE CASCADE,
  legal_first_name text NOT NULL CHECK (length(legal_first_name) BETWEEN 1 AND 100),
  legal_middle_name text CHECK (legal_middle_name IS NULL OR length(legal_middle_name) <= 100),
  legal_last_name  text NOT NULL CHECK (length(legal_last_name) BETWEEN 1 AND 100),
  date_of_birth    date CHECK (date_of_birth IS NULL OR date_of_birth <= current_date),
  -- Last four digits only. The full SSN is deliberately not collected at MVP.
  ssn_last4        text CHECK (ssn_last4 IS NULL OR ssn_last4 ~ '^[0-9]{4}$'),
  domicile_state   cairn.state_code,
  veteran_status   cairn.tri_state NOT NULL DEFAULT 'unknown',
  has_will         cairn.tri_state NOT NULL DEFAULT 'unknown',
  -- Death event fields. Null until the UC-6 step is completed.
  date_of_death    date CHECK (date_of_death IS NULL OR date_of_death <= current_date),
  place_type       text CHECK (place_type IS NULL
                               OR place_type IN ('hospital', 'hospice', 'home', 'facility', 'other')),
  facility_name    text CHECK (facility_name IS NULL OR length(facility_name) <= 200),
  city             text CHECK (city IS NULL OR length(city) <= 100),
  county           text CHECK (county IS NULL OR length(county) <= 100),
  -- Determines the issuing vital records office. Required before the journey can
  -- generate jurisdiction-matched tasks (see cairn.generate_case_tasks).
  death_state      cairn.state_code,
  CONSTRAINT death_not_before_birth
    CHECK (date_of_birth IS NULL OR date_of_death IS NULL OR date_of_death >= date_of_birth)
);

COMMENT ON COLUMN cairn.deceased.ssn_last4 IS 'Last four digits only. Sensitive. Never log.';
COMMENT ON COLUMN cairn.deceased.death_state IS 'Determines the issuing vital records office.';
COMMENT ON COLUMN cairn.cases.tasks_paused_until IS
  'Lets the product step back from task mode. Do not store distress inferences.';
