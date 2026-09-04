Runtime secrets live here and are gitignored.

Create the Grafana admin password with:
  python -c "import secrets;print(secrets.token_urlsafe(24))" > secrets/grafana_admin_password.txt

Nothing in this directory except this README and .gitkeep is ever committed.
