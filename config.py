import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-key-change-me")
    LDAP_HOST = os.environ.get("LDAP_HOST", "localhost")
    LDAP_PORT = int(os.environ.get("LDAP_PORT", 389))
    LDAP_USE_SSL = os.environ.get("LDAP_USE_SSL", "false").lower() == "true"
    LDAP_BIND_DN = os.environ.get("LDAP_BIND_DN", "cn=admin,dc=pbx,dc=com")
    LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "")
    LDAP_BASE_DN = os.environ.get("LDAP_BASE_DN", "dc=pbx,dc=com")
