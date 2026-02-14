from ldap3 import ALL, Connection, Server
from ldap3.core.exceptions import LDAPException


class LDAPClient:
    def __init__(self, config):
        self.host = config["LDAP_HOST"]
        self.port = config["LDAP_PORT"]
        self.use_ssl = config["LDAP_USE_SSL"]
        self.bind_dn = config["LDAP_BIND_DN"]
        self.bind_password = config["LDAP_BIND_PASSWORD"]
        self.base_dn = config["LDAP_BASE_DN"]

    def _connect(self):
        server = Server(self.host, port=self.port, use_ssl=self.use_ssl, get_info=ALL)
        conn = Connection(server, user=self.bind_dn, password=self.bind_password, auto_bind=True)
        return conn

    def get_all_contacts(self):
        """Return all inetOrgPerson entries sorted by displayName."""
        conn = self._connect()
        try:
            conn.search(
                self.base_dn,
                "(objectClass=inetOrgPerson)",
                attributes=["uid", "cn", "displayName", "sn", "telephoneNumber"],
            )
            contacts = []
            for entry in conn.entries:
                contacts.append(
                    {
                        "uid": str(entry.uid) if entry.uid else "",
                        "cn": str(entry.cn) if entry.cn else "",
                        "displayName": str(entry.displayName) if entry.displayName else "",
                        "sn": str(entry.sn) if entry.sn else "",
                        "telephoneNumber": str(entry.telephoneNumber) if entry.telephoneNumber else "",
                    }
                )
            contacts.sort(key=lambda c: c["displayName"].lower())
            return contacts
        finally:
            conn.unbind()

    def get_contact(self, uid):
        """Return a single contact by uid."""
        conn = self._connect()
        try:
            conn.search(
                self.base_dn,
                f"(&(objectClass=inetOrgPerson)(uid={_escape_ldap_filter(uid)}))",
                attributes=["uid", "cn", "displayName", "sn", "telephoneNumber"],
            )
            if not conn.entries:
                return None
            entry = conn.entries[0]
            return {
                "uid": str(entry.uid) if entry.uid else "",
                "cn": str(entry.cn) if entry.cn else "",
                "displayName": str(entry.displayName) if entry.displayName else "",
                "sn": str(entry.sn) if entry.sn else "",
                "telephoneNumber": str(entry.telephoneNumber) if entry.telephoneNumber else "",
            }
        finally:
            conn.unbind()

    def add_contact(self, uid, display_name, telephone):
        """Add a new inetOrgPerson entry."""
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        attributes = {
            "objectClass": ["top", "person", "organizationalPerson", "inetOrgPerson"],
            "uid": uid,
            "cn": display_name,
            "displayName": display_name,
            "sn": display_name,
            "telephoneNumber": telephone,
        }
        try:
            success = conn.add(dn, attributes=attributes)
            if not success:
                raise LDAPException(f"Failed to add contact: {conn.result['description']}")
        finally:
            conn.unbind()

    def update_contact(self, uid, display_name, telephone):
        """Update an existing contact's displayName, cn, sn, and telephoneNumber."""
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        changes = {
            "cn": [(2, [display_name])],  # MODIFY_REPLACE = 2
            "displayName": [(2, [display_name])],
            "sn": [(2, [display_name])],
            "telephoneNumber": [(2, [telephone])],
        }
        try:
            success = conn.modify(dn, changes)
            if not success:
                raise LDAPException(f"Failed to update contact: {conn.result['description']}")
        finally:
            conn.unbind()

    def delete_contact(self, uid):
        """Delete a contact by uid."""
        conn = self._connect()
        dn = f"uid={uid},{self.base_dn}"
        try:
            success = conn.delete(dn)
            if not success:
                raise LDAPException(f"Failed to delete contact: {conn.result['description']}")
        finally:
            conn.unbind()


def _escape_ldap_filter(value):
    """Escape special characters for LDAP filter to prevent injection."""
    replacements = {
        "\\": "\\5c",
        "*": "\\2a",
        "(": "\\28",
        ")": "\\29",
        "\x00": "\\00",
    }
    result = value
    # Backslash must be escaped first
    for char, escaped in replacements.items():
        result = result.replace(char, escaped)
    return result
