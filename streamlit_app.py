import streamlit as st
from datetime import date

# ---------------------------------------------------------
# PAGE CONFIGURATION
# ---------------------------------------------------------

st.set_page_config(
    page_title="Property Management",
    page_icon="🏠",
    layout="wide"
)

# ---------------------------------------------------------
# TITLE
# ---------------------------------------------------------

st.title("🏠 Property Management")
st.caption("Simple rental and monthly payment tracker")

# ---------------------------------------------------------
# SAMPLE DATA
# ---------------------------------------------------------

properties = [
    {
        "property": "Maple Apartments",
        "unit": "101",
        "tenant": "John Smith",
        "rent": 1500,
        "paid": 1500
    },
    {
        "property": "Maple Apartments",
        "unit": "102",
        "tenant": "Mary Jones",
        "rent": 1400,
        "paid": 1000
    },
    {
        "property": "Oak Street Apartments",
        "unit": "201",
        "tenant": "Robert Lee",
        "rent": 1600,
        "paid": 0
    },
]

# ---------------------------------------------------------
# CALCULATIONS
# ---------------------------------------------------------

expected_rent = sum(x["rent"] for x in properties)
collected_rent = sum(x["paid"] for x in properties)
outstanding_rent = expected_rent - collected_rent

collection_rate = (
    collected_rent / expected_rent * 100
    if expected_rent > 0
    else 0
)

# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------

st.subheader("August 2026")

col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Properties",
    len(set(x["property"] for x in properties))
)

col2.metric(
    "Units",
    len(properties)
)

col3.metric(
    "Expected Rent",
    f"${expected_rent:,.0f}"
)

col4.metric(
    "Collected",
    f"${collected_rent:,.0f}"
)

st.divider()

col1, col2, col3 = st.columns(3)

col1.metric(
    "Outstanding",
    f"${outstanding_rent:,.0f}"
)

col2.metric(
    "Collection Rate",
    f"{collection_rate:.1f}%"
)

overdue_count = sum(
    1 for x in properties
    if x["paid"] == 0
)

col3.metric(
    "Overdue Tenants",
    overdue_count
)

# ---------------------------------------------------------
# RENT TABLE
# ---------------------------------------------------------

st.subheader("Monthly Rent")

for tenant in properties:

    balance = tenant["rent"] - tenant["paid"]

    if balance == 0:
        status = "✅ PAID"
    elif tenant["paid"] > 0:
        status = "⚠️ PARTIAL"
    else:
        status = "🔴 OVERDUE"

    st.write(
        f"**{tenant['tenant']}** — "
        f"{tenant['property']} — Unit {tenant['unit']}"
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.write(f"Rent: **${tenant['rent']:,.0f}**")
    c2.write(f"Paid: **${tenant['paid']:,.0f}**")
    c3.write(f"Balance: **${balance:,.0f}**")
    c4.write(status)

    st.divider()

# ---------------------------------------------------------
# RECORD PAYMENT
# ---------------------------------------------------------

st.subheader("💳 Record Payment")

tenant_names = [
    x["tenant"]
    for x in properties
]

selected_tenant = st.selectbox(
    "Tenant",
    tenant_names
)

payment_amount = st.number_input(
    "Payment Amount",
    min_value=0.0,
    step=50.0
)

payment_date = st.date_input(
    "Payment Date",
    value=date.today()
)

payment_method = st.selectbox(
    "Payment Method",
    [
        "ACH",
        "Check",
        "Cash",
        "Credit Card",
        "Other"
    ]
)

if st.button("Save Payment", type="primary"):

    st.success(
        f"Payment of ${payment_amount:,.2f} "
        f"recorded for {selected_tenant}."
    )

    st.write(f"Payment date: {payment_date}")
    st.write(f"Payment method: {payment_method}")