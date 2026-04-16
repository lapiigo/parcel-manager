"""
Role permission matrix:
  super_admin : full access, user management including delete
  admin       : full access, user management (no delete super_admin)
  manager     : parcels/orders/suppliers/clients CRUD — no stats/reports/users
  client      : client portal only (own parcels, reports, balance)
"""

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "admin": "Admin",
    "manager": "Manager",
    "client": "Client",
}

ROLE_BADGE_COLORS = {
    "super_admin": "bg-red-100 text-red-700",
    "admin": "bg-purple-100 text-purple-700",
    "manager": "bg-blue-100 text-blue-700",
    "client": "bg-green-100 text-green-700",
}


def can(user, action: str) -> bool:
    """Check if user has permission for a given action."""
    if not user:
        return False
    role = user.role

    rules = {
        # Dashboard/stats
        "view_dashboard": ["super_admin", "admin", "manager"],
        "view_statistics": ["super_admin", "admin"],
        # Parcels
        "view_parcels": ["super_admin", "admin", "manager"],
        "create_parcel": ["super_admin", "admin", "manager"],
        "edit_parcel": ["super_admin", "admin", "manager"],
        "delete_parcel": ["super_admin", "admin"],
        "change_parcel_status": ["super_admin", "admin", "manager"],
        # Orders
        "view_orders": ["super_admin", "admin", "manager"],
        "create_order": ["super_admin", "admin", "manager"],
        "edit_order": ["super_admin", "admin", "manager"],
        "delete_order": ["super_admin", "admin"],
        "view_financials": ["super_admin", "admin"],
        # Suppliers
        "view_suppliers": ["super_admin", "admin", "manager"],
        "create_supplier": ["super_admin", "admin", "manager"],
        "edit_supplier": ["super_admin", "admin", "manager"],
        "delete_supplier": ["super_admin", "admin"],
        # Clients
        "view_clients": ["super_admin", "admin", "manager"],
        "create_client": ["super_admin", "admin", "manager"],
        "edit_client": ["super_admin", "admin", "manager"],
        "delete_client": ["super_admin", "admin"],
        # Reports
        "view_reports": ["super_admin", "admin"],
        "create_report": ["super_admin", "admin"],
        "delete_report": ["super_admin"],
        # User management
        "view_users": ["super_admin", "admin"],
        "create_user": ["super_admin", "admin"],
        "edit_user": ["super_admin", "admin"],
        "delete_user": ["super_admin"],
        "manage_roles": ["super_admin"],
        # Client portal
        "use_portal": ["client"],
    }

    return role in rules.get(action, [])
