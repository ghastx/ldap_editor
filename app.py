from flask import Flask, flash, redirect, render_template, request, url_for
from ldap3.core.exceptions import LDAPException

from audit_log import get_log, log_action
from config import Config
from ldap_client import LDAPClient

app = Flask(__name__)
app.config.from_object(Config)

ldap = LDAPClient(
    {
        "LDAP_HOST": app.config["LDAP_HOST"],
        "LDAP_PORT": app.config["LDAP_PORT"],
        "LDAP_USE_SSL": app.config["LDAP_USE_SSL"],
        "LDAP_BIND_DN": app.config["LDAP_BIND_DN"],
        "LDAP_BIND_PASSWORD": app.config["LDAP_BIND_PASSWORD"],
        "LDAP_BASE_DN": app.config["LDAP_BASE_DN"],
    }
)


@app.route("/")
def index():
    search = request.args.get("q", "").strip()
    try:
        contacts = ldap.get_all_contacts()
    except LDAPException as e:
        flash(f"Errore di connessione LDAP: {e}", "danger")
        contacts = []

    if search:
        q = search.lower()
        contacts = [
            c
            for c in contacts
            if q in c["displayName"].lower() or q in c["telephoneNumber"]
        ]

    return render_template("index.html", contacts=contacts, search=search)


@app.route("/add", methods=["GET", "POST"])
def add_contact():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        telephone = request.form.get("telephone", "").strip()

        if not display_name or not telephone:
            flash("Nome e numero di telefono sono obbligatori.", "danger")
            return render_template(
                "add.html", display_name=display_name, telephone=telephone
            )

        # Use displayName as uid (same pattern as existing entries)
        uid = display_name.replace(" ", "")
        try:
            ldap.add_contact(uid, display_name, telephone)
            log_action(
                "aggiunto", uid,
                f"Nome: {display_name}, Tel: {telephone}",
                request.remote_addr,
            )
            flash(f"Contatto '{display_name}' aggiunto con successo.", "success")
            return redirect(url_for("index"))
        except LDAPException as e:
            flash(f"Errore nell'aggiunta del contatto: {e}", "danger")
            return render_template(
                "add.html", display_name=display_name, telephone=telephone
            )

    return render_template("add.html", display_name="", telephone="")


@app.route("/edit/<uid>", methods=["GET", "POST"])
def edit_contact(uid):
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        telephone = request.form.get("telephone", "").strip()

        if not display_name or not telephone:
            flash("Nome e numero di telefono sono obbligatori.", "danger")
            return render_template(
                "edit.html",
                contact={"uid": uid, "displayName": display_name, "telephoneNumber": telephone},
            )

        try:
            old_contact = ldap.get_contact(uid)
            ldap.update_contact(uid, display_name, telephone)
            # Build details showing what changed
            changes = []
            if old_contact and old_contact["displayName"] != display_name:
                changes.append(f"Nome: {old_contact['displayName']} -> {display_name}")
            if old_contact and old_contact["telephoneNumber"] != telephone:
                changes.append(f"Tel: {old_contact['telephoneNumber']} -> {telephone}")
            log_action(
                "modificato", uid,
                "; ".join(changes) if changes else "Nessuna modifica rilevata",
                request.remote_addr,
            )
            flash(f"Contatto '{display_name}' aggiornato con successo.", "success")
            return redirect(url_for("index"))
        except LDAPException as e:
            flash(f"Errore nell'aggiornamento del contatto: {e}", "danger")
            return render_template(
                "edit.html",
                contact={"uid": uid, "displayName": display_name, "telephoneNumber": telephone},
            )

    try:
        contact = ldap.get_contact(uid)
    except LDAPException as e:
        flash(f"Errore nel caricamento del contatto: {e}", "danger")
        return redirect(url_for("index"))

    if not contact:
        flash("Contatto non trovato.", "danger")
        return redirect(url_for("index"))

    return render_template("edit.html", contact=contact)


@app.route("/delete/<uid>", methods=["POST"])
def delete_contact(uid):
    try:
        contact = ldap.get_contact(uid)
        ldap.delete_contact(uid)
        detail = ""
        if contact:
            detail = f"Nome: {contact['displayName']}, Tel: {contact['telephoneNumber']}"
        log_action("eliminato", uid, detail, request.remote_addr)
        flash("Contatto eliminato con successo.", "success")
    except LDAPException as e:
        flash(f"Errore nell'eliminazione del contatto: {e}", "danger")
    return redirect(url_for("index"))


@app.route("/log")
def audit_log():
    entries = get_log()
    return render_template("log.html", entries=entries)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
