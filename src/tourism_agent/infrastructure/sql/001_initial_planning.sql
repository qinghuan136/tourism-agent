CREATE TABLE tourism_agent.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tourism_agent.trips (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES tourism_agent.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);

CREATE INDEX idx_trips_user_created
    ON tourism_agent.trips (user_id, created_at DESC);

CREATE TABLE tourism_agent.conversation_messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id uuid NOT NULL REFERENCES tourism_agent.trips(id),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL CHECK (btrim(content) <> ''),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversation_messages_trip_recent
    ON tourism_agent.conversation_messages (trip_id, id DESC);

CREATE TABLE tourism_agent.trip_contexts (
    trip_id uuid PRIMARY KEY REFERENCES tourism_agent.trips(id),
    data jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(data) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tourism_agent.current_itineraries (
    trip_id uuid PRIMARY KEY REFERENCES tourism_agent.trips(id),
    content text NOT NULL CHECK (btrim(content) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
