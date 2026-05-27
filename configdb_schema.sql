CREATE DATABASE IF NOT EXISTS configdb;

CREATE TABLE IF NOT EXISTS configdb.connection_profiles (
  name VARCHAR(128) PRIMARY KEY,
  label VARCHAR(255) NOT NULL,
  profile_json JSON NOT NULL,
  profile_management BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS configdb.local_users (
  username VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  password_salt VARCHAR(64) NOT NULL,
  password_hash CHAR(64) NOT NULL,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  force_password_change BOOLEAN NOT NULL DEFAULT FALSE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS configdb.local_groups (
  group_name VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  is_admin BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS configdb.local_user_groups (
  username VARCHAR(128) NOT NULL,
  group_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (username, group_name)
);

CREATE TABLE IF NOT EXISTS configdb.profile_assignments (
  profile_name VARCHAR(128) NOT NULL,
  subject_type ENUM('user','group') NOT NULL,
  subject_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (profile_name, subject_type, subject_name)
);

INSERT INTO configdb.local_groups (group_name, display_name, is_admin)
VALUES ('Admin', 'Admin', TRUE), ('General User', 'General User', FALSE)
ON DUPLICATE KEY UPDATE display_name=VALUES(display_name), is_admin=VALUES(is_admin);

INSERT INTO configdb.local_users (username, display_name, password_salt, password_hash, is_admin, force_password_change)
VALUES ('localadmin', 'Local Admin', 'bootstrap', SHA2('bootstrap:localadmin', 256), TRUE, TRUE)
ON DUPLICATE KEY UPDATE username=username;

INSERT IGNORE INTO configdb.local_user_groups (username, group_name)
VALUES ('localadmin', 'Admin');
