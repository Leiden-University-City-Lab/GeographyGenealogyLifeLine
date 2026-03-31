START TRANSACTION;

CREATE TABLE person (
    person_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    affix VARCHAR(255),
    alternative_last_name VARCHAR(255)
);

CREATE TABLE location (
    location_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country VARCHAR(255),
    city VARCHAR(255),
    latitude DOUBLE,
    longitude DOUBLE
);

CREATE TABLE event_type (
    event_type_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type_name VARCHAR(100) NOT NULL,
    UNIQUE (event_type_name)
);

CREATE TABLE event (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id BIGINT NOT NULL,
    location_id BIGINT,
    event_type_id BIGINT NOT NULL,
    begin_date DATE,
    end_date DATE,
    description VARCHAR(255),
    FOREIGN KEY (person_id) REFERENCES person(person_id),
    FOREIGN KEY (location_id) REFERENCES location(location_id),
    FOREIGN KEY (event_type_id) REFERENCES event_type(event_type_id)
);


COMMIT;
