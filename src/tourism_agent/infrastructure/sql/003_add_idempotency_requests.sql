CREATE TABLE tourism_agent.idempotency_requests (
    idempotency_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES tourism_agent.users(id),
    trip_id uuid NOT NULL REFERENCES tourism_agent.trips(id),
    request_hash text NOT NULL CHECK (btrim(request_hash) <> ''),
    status text NOT NULL CHECK (
        status IN ('processing', 'completed', 'failed', 'cancelled')
    ),
    response_status integer CHECK (
        response_status IS NULL OR response_status BETWEEN 100 AND 599
    ),
    response_body jsonb CHECK (
        response_body IS NULL OR jsonb_typeof(response_body) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'processing' AND response_status IS NULL AND response_body IS NULL)
        OR
        (status <> 'processing' AND response_status IS NOT NULL AND response_body IS NOT NULL)
    )
);

CREATE INDEX idx_idempotency_requests_trip_created
    ON tourism_agent.idempotency_requests (trip_id, created_at DESC);
