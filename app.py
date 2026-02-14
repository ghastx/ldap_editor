from flask import Flask, flash, redirect, render_template, request, url_for
from ldap3.core.exceptions import LDAPException

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
            ldap.update_contact(uid, display_name, telephone)
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
        ldap.delete_contact(uid)
        flash("Contatto eliminato con successo.", "success")
    except LDAPException as e:
        flash(f"Errore nell'eliminazione del contatto: {e}", "danger")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
