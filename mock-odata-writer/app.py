"""Mock OData V4 Service with full CRUD support.

Provides Products, Orders, and Suppliers entities for testing
write operations (POST, PATCH, DELETE) in the orchestrator.
"""
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# In-memory data store
_next_id = {
    "Products": 100,
    "Orders": 2000,
    "Order_Details": 5000,
    "Suppliers": 50,
}

_products = [
    {"ProductID": 1, "ProductName": "Chai", "SupplierID": 1, "CategoryID": 1, "QuantityPerUnit": "10 boxes x 20 bags", "UnitPrice": 18.00, "UnitsInStock": 39, "UnitsOnOrder": 0, "ReorderLevel": 10, "Discontinued": False},
    {"ProductID": 2, "ProductName": "Chang", "SupplierID": 1, "CategoryID": 1, "QuantityPerUnit": "24 - 12 oz bottles", "UnitPrice": 19.00, "UnitsOnStock": 17, "UnitsOnOrder": 40, "ReorderLevel": 25, "Discontinued": False},
    {"ProductID": 3, "ProductName": "Aniseed Syrup", "SupplierID": 1, "CategoryID": 2, "QuantityPerUnit": "12 - 550 ml bottles", "UnitPrice": 10.00, "UnitsInStock": 13, "UnitsOnOrder": 70, "ReorderLevel": 25, "Discontinued": False},
    {"ProductID": 4, "ProductName": "Chef Anton's Cajun Seasoning", "SupplierID": 2, "CategoryID": 2, "QuantityPerUnit": "48 - 6 oz jars", "UnitPrice": 22.00, "UnitsInStock": 53, "UnitsOnOrder": 0, "ReorderLevel": 0, "Discontinued": False},
    {"ProductID": 5, "ProductName": "Chef Anton's Gumbo Mix", "SupplierID": 2, "CategoryID": 2, "QuantityPerUnit": "36 boxes", "UnitPrice": 21.35, "UnitsInStock": 0, "UnitsOnOrder": 0, "ReorderLevel": 0, "Discontinued": True},
]

_orders = [
    {"OrderID": 10248, "CustomerID": "VINET", "EmployeeID": 5, "OrderDate": "1996-07-04", "RequiredDate": "1996-08-01", "ShippedDate": "1996-07-16", "ShipVia": 3, "Freight": 32.38, "ShipName": "Vins et alcools Chevalier", "ShipCity": "Reims", "ShipCountry": "France"},
    {"OrderID": 10249, "CustomerID": "TOMSP", "EmployeeID": 6, "OrderDate": "1996-07-05", "RequiredDate": "1996-08-16", "ShippedDate": "1996-07-10", "ShipVia": 1, "Freight": 11.61, "ShipName": "Toms Spezialitaten", "ShipCity": "Munster", "ShipCountry": "Germany"},
]

_order_details = [
    {"OrderID": 10248, "ProductID": 11, "UnitPrice": 14.00, "Quantity": 12, "Discount": 0.0},
    {"OrderID": 10248, "ProductID": 42, "UnitPrice": 9.80, "Quantity": 10, "Discount": 0.0},
    {"OrderID": 10249, "ProductID": 14, "UnitPrice": 18.60, "Quantity": 9, "Discount": 0.0},
]

_suppliers = [
    {"SupplierID": 1, "CompanyName": "Exotic Liquids", "ContactName": "Charlotte Cooper", "ContactTitle": "Purchasing Manager", "Address": "49 Gilbert St.", "City": "London", "Region": None, "PostalCode": "EC1 4SD", "Country": "UK", "Phone": "(171) 555-2222"},
    {"SupplierID": 2, "CompanyName": "New Orleans Cajun Delights", "ContactName": "Shelley Burke", "ContactTitle": "Order Administrator", "Address": "P.O. Box 78934", "City": "New Orleans", "Region": "LA", "PostalCode": "70117", "Country": "USA", "Phone": "(100) 555-4822"},
]

_entity_sets = {
    "Products": {"data": _products, "key": "ProductID", "type": "Demo.ProductsType"},
    "Orders": {"data": _orders, "key": "OrderID", "type": "Demo.OrdersType"},
    "Order_Details": {"data": _order_details, "key": None, "type": "Demo.Order_DetailsType"},
    "Suppliers": {"data": _suppliers, "key": "SupplierID", "type": "Demo.SuppliersType"},
}


METADATA_CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" xmlns:edm="http://docs.oasis-open.org/odata/ns/edm" xmlns:sap="http://www.sap.com/Protocols/SAPData">
  <edmx:DataServices>
    <Schema Namespace="Demo" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="ProductsType" sap:label="Product">
        <Key><PropertyRef Name="ProductID"/></Key>
        <Property Name="ProductID" Type="Edm.Int32" Nullable="false" sap:label="Product ID"/>
        <Property Name="ProductName" Type="Edm.String" MaxLength="40" sap:label="Product Name"/>
        <Property Name="SupplierID" Type="Edm.Int32" sap:label="Supplier ID"/>
        <Property Name="CategoryID" Type="Edm.Int32" sap:label="Category ID"/>
        <Property Name="QuantityPerUnit" Type="Edm.String" MaxLength="20" sap:label="Quantity Per Unit"/>
        <Property Name="UnitPrice" Type="Edm.Decimal" Precision="19" Scale="4" sap:label="Unit Price"/>
        <Property Name="UnitsInStock" Type="Edm.Int16" sap:label="Units In Stock"/>
        <Property Name="UnitsOnOrder" Type="Edm.Int16" sap:label="Units On Order"/>
        <Property Name="ReorderLevel" Type="Edm.Int16" sap:label="Reorder Level"/>
        <Property Name="Discontinued" Type="Edm.Boolean" sap:label="Discontinued"/>
      </EntityType>
      <EntityType Name="OrdersType" sap:label="Order">
        <Key><PropertyRef Name="OrderID"/></Key>
        <Property Name="OrderID" Type="Edm.Int32" Nullable="false" sap:label="Order ID"/>
        <Property Name="CustomerID" Type="Edm.String" MaxLength="5" sap:label="Customer ID"/>
        <Property Name="EmployeeID" Type="Edm.Int32" sap:label="Employee ID"/>
        <Property Name="OrderDate" Type="Edm.Date" sap:label="Order Date"/>
        <Property Name="RequiredDate" Type="Edm.Date" sap:label="Required Date"/>
        <Property Name="ShippedDate" Type="Edm.Date" sap:label="Shipped Date"/>
        <Property Name="ShipVia" Type="Edm.Int32" sap:label="Ship Via"/>
        <Property Name="Freight" Type="Edm.Decimal" Precision="19" Scale="4" sap:label="Freight"/>
        <Property Name="ShipName" Type="Edm.String" MaxLength="40" sap:label="Ship Name"/>
        <Property Name="ShipCity" Type="Edm.String" MaxLength="15" sap:label="Ship City"/>
        <Property Name="ShipCountry" Type="Edm.String" MaxLength="15" sap:label="Ship Country"/>
      </EntityType>
      <EntityType Name="Order_DetailsType" sap:label="Order Detail">
        <Key><PropertyRef Name="OrderID"/><PropertyRef Name="ProductID"/></Key>
        <Property Name="OrderID" Type="Edm.Int32" Nullable="false" sap:label="Order ID"/>
        <Property Name="ProductID" Type="Edm.Int32" Nullable="false" sap:label="Product ID"/>
        <Property Name="UnitPrice" Type="Edm.Decimal" Precision="19" Scale="4" sap:label="Unit Price"/>
        <Property Name="Quantity" Type="Edm.Int16" sap:label="Quantity"/>
        <Property Name="Discount" Type="Edm.Single" sap:label="Discount"/>
      </EntityType>
      <EntityType Name="SuppliersType" sap:label="Supplier">
        <Key><PropertyRef Name="SupplierID"/></Key>
        <Property Name="SupplierID" Type="Edm.Int32" Nullable="false" sap:label="Supplier ID"/>
        <Property Name="CompanyName" Type="Edm.String" MaxLength="40" sap:label="Company Name"/>
        <Property Name="ContactName" Type="Edm.String" MaxLength="30" sap:label="Contact Name"/>
        <Property Name="ContactTitle" Type="Edm.String" MaxLength="30" sap:label="Contact Title"/>
        <Property Name="Address" Type="Edm.String" MaxLength="60" sap:label="Address"/>
        <Property Name="City" Type="Edm.String" MaxLength="15" sap:label="City"/>
        <Property Name="Region" Type="Edm.String" MaxLength="15" sap:label="Region"/>
        <Property Name="PostalCode" Type="Edm.String" MaxLength="10" sap:label="Postal Code"/>
        <Property Name="Country" Type="Edm.String" MaxLength="15" sap:label="Country"/>
        <Property Name="Phone" Type="Edm.String" MaxLength="24" sap:label="Phone"/>
      </EntityType>
      <EntityContainer Name="DemoService">
        <EntitySet Name="Products" EntityType="Demo.ProductsType"/>
        <EntitySet Name="Orders" EntityType="Demo.OrdersType"/>
        <EntitySet Name="Order_Details" EntityType="Demo.Order_DetailsType"/>
        <EntitySet Name="Suppliers" EntityType="Demo.SuppliersType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


@app.route("/DemoService/$metadata", methods=["GET"])
def metadata():
    return Response(METADATA_CSDL, content_type="application/xml")


@app.route("/DemoService", methods=["GET"])
def service_document():
    return jsonify({
        "@odata.context": "$metadata",
        "value": [
            {"name": "Products", "kind": "EntitySet", "url": "Products"},
            {"name": "Orders", "kind": "EntitySet", "url": "Orders"},
            {"name": "Order_Details", "kind": "EntitySet", "url": "Order_Details"},
            {"name": "Suppliers", "kind": "EntitySet", "url": "Suppliers"},
        ]
    })


@app.route("/DemoService/<entity_set>", methods=["GET"])
def get_entities(entity_set):
    if entity_set not in _entity_sets:
        return jsonify({"error": f"Unknown entity set: {entity_set}"}), 404
    es = _entity_sets[entity_set]
    data = list(es["data"])
    top = request.args.get("$top", None, type=int)
    skip = request.args.get("$skip", 0, type=int)
    count = request.args.get("$count", None)
    if top:
        data = data[skip:skip + top]
    elif skip:
        data = data[skip:]
    result = {
        "@odata.context": f"$metadata#{entity_set}",
        "value": data,
    }
    if count == "true":
        result["@odata.count"] = len(es["data"])
    resp = jsonify(result)
    resp.headers["OData-Version"] = "4.0"
    return resp


@app.route("/DemoService/<entity_set>(<int:key_value>)", methods=["GET"])
@app.route("/DemoService/<entity_set>('<string:key_value>')", methods=["GET"])
@app.route("/DemoService/<entity_set>('{key_value}')", methods=["GET"])
def get_entity(entity_set, key_value):
    if entity_set not in _entity_sets:
        return jsonify({"error": f"Unknown entity set: {entity_set}"}), 404
    es = _entity_sets[entity_set]
    key = es["key"]
    for item in es["data"]:
        if str(item.get(key)) == str(key_value):
            return jsonify({
                "@odata.context": f"$metadata#{entity_set}",
                **item,
            })
    return jsonify({"error": "Not found"}), 404


@app.route("/DemoService/<entity_set>", methods=["POST"])
def create_entity(entity_set):
    if entity_set not in _entity_sets:
        return jsonify({"error": f"Unknown entity set: {entity_set}"}), 404
    es = _entity_sets[entity_set]
    body = request.get_json(force=True, silent=True) or {}
    if not body:
        return jsonify({"error": "No request body"}), 400
    new_id = _next_id.get(entity_set, 9999) + 1
    _next_id[entity_set] = new_id
    key = es["key"]
    if key:
        body[key] = new_id
    es["data"].append(body)
    resp = jsonify(body)
    resp.status_code = 201
    resp.headers["OData-Version"] = "4.0"
    location = f"/DemoService/{entity_set}"
    if key:
        location += f"({new_id})"
    resp.headers["Location"] = location
    return resp


@app.route("/DemoService/<entity_set>(<int:key_value>)", methods=["PATCH"])
@app.route("/DemoService/<entity_set>(<string:key_value>)", methods=["PATCH"])
def update_entity(entity_set, key_value):
    if entity_set not in _entity_sets:
        return jsonify({"error": f"Unknown entity set: {entity_set}"}), 404
    es = _entity_sets[entity_set]
    key = es["key"]
    body = request.get_json(force=True, silent=True) or {}
    for item in es["data"]:
        if str(item.get(key)) == str(key_value):
            item.update(body)
            resp = jsonify(item)
            resp.headers["OData-Version"] = "4.0"
            return resp
    return jsonify({"error": "Not found"}), 404


@app.route("/DemoService/<entity_set>(<int:key_value>)", methods=["DELETE"])
@app.route("/DemoService/<entity_set>(<string:key_value>)", methods=["DELETE"])
def delete_entity(entity_set, key_value):
    if entity_set not in _entity_sets:
        return jsonify({"error": f"Unknown entity set: {entity_set}"}), 404
    es = _entity_sets[entity_set]
    key = es["key"]
    for i, item in enumerate(es["data"]):
        if str(item.get(key)) == str(key_value):
            es["data"].pop(i)
            return Response(status=204, headers={"OData-Version": "4.0"})
    return jsonify({"error": "Not found"}), 404


@app.route("/DemoService/<entity_set>(<int:key_value>)", methods=["POST"])
def action_on_entity(entity_set, key_value):
    return jsonify({"error": "Actions not supported"}), 501


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "mock-odata-writer"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
