import json

PERMISSION_GROUPS = [
    ("Parcels", [
        ("view_parcels",    "View parcels"),
        ("edit_parcel",     "Create & edit parcels"),
        ("delete_parcel",   "Delete parcels"),
    ]),
    ("Orders", [
        ("view_orders",     "View orders"),
        ("edit_order",      "Create & edit orders"),
        ("delete_order",    "Delete orders"),
        ("view_financials", "View financial data"),
    ]),
    ("Reports", [
        ("view_reports",    "View & create reports"),
        ("delete_report",   "Delete reports"),
    ]),
    ("Companies", [
        ("view_clients",    "View companies"),
        ("edit_client",     "Create & edit companies"),
        ("delete_client",   "Delete companies"),
    ]),
    ("Suppliers", [
        ("view_suppliers",  "View suppliers"),
        ("edit_supplier",   "Create & edit suppliers"),
        ("delete_supplier", "Delete suppliers"),
    ]),
    ("Workspace", [
        ("view_workspace",  "Workspace (tasks & meetings)"),
    ]),
    ("Administration", [
        ("view_users",      "User management"),
    ]),
]

ALL_PERMISSIONS = [p for _, group in PERMISSION_GROUPS for p, _ in group]

# Maps legacy/derived action names → stored permission key
_DERIVED = {
    "create_parcel":        "edit_parcel",
    "change_parcel_status": "edit_parcel",
    "view_statistics":      "view_financials",
    "view_dashboard":       None,  # any staff user
    "create_order":         "edit_order",
    "create_supplier":      "edit_supplier",
    "create_client":        "edit_client",
    "create_report":        "view_reports",
    "create_user":          "view_users",
    "edit_user":            "view_users",
    "delete_user":          "view_users",
    "manage_roles":         "view_users",
    "use_portal":           None,   # client-only, handled separately
}

ROLE_BADGE_COLORS = {
    "super_admin": "bg-red-100 text-red-700",
    "staff":       "bg-blue-100 text-blue-700",
    "admin":       "bg-blue-100 text-blue-700",   # legacy
    "manager":     "bg-blue-100 text-blue-700",   # legacy
    "client":      "bg-green-100 text-green-700",
}

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "staff":       "Staff",
    "admin":       "Staff",    # legacy display
    "manager":     "Staff",    # legacy display
    "client":      "Client",
}


def can(user, action: str) -> bool:
    if not user:
        return False
    if user.role == "super_admin":
        return True
    if user.role == "client":
        return action == "use_portal"
    # staff (and legacy admin/manager) — check stored permissions
    try:
        perms: set[str] = set(json.loads(user.permissions or "[]"))
    except Exception:
        perms = set()

    if action == "use_portal":
        return False
    if action == "view_dashboard":
        return bool(perms)  # any permission = dashboard access
    check = _DERIVED.get(action, action)
    if check is None:
        return True   # derived None means "always for staff"
    return check in perms
