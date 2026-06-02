"""Local mock OData v4 service for testing.

Implements a minimal subset of the OData v4 protocol:
  - GET /$metadata  -> CSDL XML
  - GET /<EntitySet>  -> JSON value list
  - GET /<EntitySet>(id)  -> JSON object
  - GET /<EntitySet>?$select=...&$filter=...&$top=...&$skip=...&$orderby=...&$expand=...
  - GET /<EntitySet>?$count=true  -> adds @odata.count

Run with:
    uvicorn sample_odata_service.app:app --port 5000 --reload
"""
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="Sample OData v4 Service")


CSDL = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0"
  xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Sample.Northwind"
      xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="Customer">
        <Key>
          <PropertyRef Name="CustomerID"/>
        </Key>
        <Property Name="CustomerID" Type="Edm.String" Nullable="false"/>
        <Property Name="CompanyName" Type="Edm.String"/>
        <Property Name="ContactName" Type="Edm.String"/>
        <Property Name="Country" Type="Edm.String"/>
        <Property Name="City" Type="Edm.String"/>
        <Property Name="Phone" Type="Edm.String"/>
        <NavigationProperty Name="Orders" Type="Collection(Sample.Northwind.Order)" Partner="Customer"/>
      </EntityType>
      <EntityType Name="Order">
        <Key>
          <PropertyRef Name="OrderID"/>
        </Key>
        <Property Name="OrderID" Type="Edm.Int32" Nullable="false"/>
        <Property Name="CustomerID" Type="Edm.String"/>
        <Property Name="OrderDate" Type="Edm.DateTimeOffset"/>
        <Property Name="TotalAmount" Type="Edm.Decimal"/>
        <Property Name="Status" Type="Edm.String"/>
        <NavigationProperty Name="Customer" Type="Sample.Northwind.Customer" Partner="Orders"/>
        <NavigationProperty Name="Products" Type="Collection(Sample.Northwind.Product)" Partner="Orders"/>
      </EntityType>
      <EntityType Name="Product">
        <Key>
          <PropertyRef Name="ProductID"/>
        </Key>
        <Property Name="ProductID" Type="Edm.Int32" Nullable="false"/>
        <Property Name="ProductName" Type="Edm.String"/>
        <Property Name="Category" Type="Edm.String"/>
        <Property Name="UnitPrice" Type="Edm.Decimal"/>
        <Property Name="UnitsInStock" Type="Edm.Int32"/>
      </EntityType>
      <EntityContainer Name="SampleContainer">
        <EntitySet Name="Customers" EntityType="Sample.Northwind.Customer">
          <NavigationPropertyBinding Path="Orders" Target="Orders"/>
        </EntitySet>
        <EntitySet Name="Orders" EntityType="Sample.Northwind.Order">
          <NavigationPropertyBinding Path="Customer" Target="Customers"/>
          <NavigationPropertyBinding Path="Products" Target="Products"/>
        </EntitySet>
        <EntitySet Name="Products" EntityType="Sample.Northwind.Product"/>
        <Association Name="Customer_Orders" >
          <End Type="Sample.Northwind.Customer" Role="Customer" Multiplicity="1"/>
          <End Type="Sample.Northwind.Order" Role="Orders" Multiplicity="*"/>
        </Association>
        <Association Name="Order_Products" >
          <End Type="Sample.Northwind.Order" Role="Order" Multiplicity="1"/>
          <End Type="Sample.Northwind.Product" Role="Products" Multiplicity="*"/>
        </Association>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


CUSTOMERS: List[Dict[str, Any]] = [
    {"CustomerID": "ALFKI", "CompanyName": "Alfreds Futterkiste", "ContactName": "Maria Anders", "Country": "Germany", "City": "Berlin", "Phone": "030-0074321"},
    {"CustomerID": "ANATR", "CompanyName": "Ana Trujillo Emparedados y helados", "ContactName": "Ana Trujillo", "Country": "Mexico", "City": "México D.F.", "Phone": "(5) 555-4729"},
    {"CustomerID": "ANTON", "CompanyName": "Antonio Moreno Taquería", "ContactName": "Antonio Moreno", "Country": "Mexico", "City": "México D.F.", "Phone": "(5) 555-3932"},
    {"CustomerID": "AROUT", "CompanyName": "Around the Horn", "ContactName": "Thomas Hardy", "Country": "UK", "City": "London", "Phone": "(171) 555-7788"},
    {"CustomerID": "BERGS", "CompanyName": "Berglunds snabbköp", "ContactName": "Christina Berglund", "Country": "Sweden", "City": "Luleå", "Phone": "0921-12 34 65"},
    {"CustomerID": "BLAUS", "CompanyName": "Blauer See Delikatessen", "ContactName": "Hanna Moos", "Country": "Germany", "City": "Mannheim", "Phone": "0621-08460"},
    {"CustomerID": "BLONP", "CompanyName": "Blondesddsl père et fils", "ContactName": "Frédérique Citeaux", "Country": "France", "City": "Strasbourg", "Phone": "88.60.15.31"},
    {"CustomerID": "BOLID", "CompanyName": "Bólido Comidas preparadas", "ContactName": "Martín Sommer", "Country": "Spain", "City": "Madrid", "Phone": "(91) 555 22 82"},
    {"CustomerID": "BONAP", "CompanyName": "Bon app'", "ContactName": "Laurence Lebihan", "Country": "France", "City": "Marseille", "Phone": "91.24.45.40"},
    {"CustomerID": "BOTTM", "CompanyName": "Bottom-Dollar Markets", "ContactName": "Elizabeth Lincoln", "Country": "Canada", "City": "Tsawassen", "Phone": "(604) 555-4729"},
]


ORDERS: List[Dict[str, Any]] = [
    {"OrderID": 10248, "CustomerID": "VINET", "OrderDate": "2024-01-04T00:00:00Z", "TotalAmount": 32.38, "Status": "Shipped"},
    {"OrderID": 10249, "CustomerID": "TOMSP", "OrderDate": "2024-01-05T00:00:00Z", "TotalAmount": 11.61, "Status": "Shipped"},
    {"OrderID": 10250, "CustomerID": "HANAR", "OrderDate": "2024-01-08T00:00:00Z", "TotalAmount": 65.83, "Status": "Pending"},
    {"OrderID": 10251, "CustomerID": "VICTE", "OrderDate": "2024-01-08T00:00:00Z", "TotalAmount": 41.34, "Status": "Shipped"},
    {"OrderID": 10252, "CustomerID": "SUPRD", "OrderDate": "2024-01-09T00:00:00Z", "TotalAmount": 51.30, "Status": "Cancelled"},
    {"OrderID": 10253, "CustomerID": "HANAR", "OrderDate": "2024-01-10T00:00:00Z", "TotalAmount": 58.17, "Status": "Shipped"},
    {"OrderID": 10254, "CustomerID": "CHOPS", "OrderDate": "2024-01-11T00:00:00Z", "TotalAmount": 22.98, "Status": "Shipped"},
    {"OrderID": 10255, "CustomerID": "RICSU", "OrderDate": "2024-01-12T00:00:00Z", "TotalAmount": 148.33, "Status": "Pending"},
    {"OrderID": 10256, "CustomerID": "WELLI", "OrderDate": "2024-01-15T00:00:00Z", "TotalAmount": 13.97, "Status": "Shipped"},
    {"OrderID": 10257, "CustomerID": "HILAA", "OrderDate": "2024-01-16T00:00:00Z", "TotalAmount": 81.91, "Status": "Shipped"},
    {"OrderID": 10258, "CustomerID": "ERNSH", "OrderDate": "2024-01-17T00:00:00Z", "TotalAmount": 140.51, "Status": "Shipped"},
    {"OrderID": 10259, "CustomerID": "CENTC", "OrderDate": "2024-01-18T00:00:00Z", "TotalAmount": 3.25, "Status": "Shipped"},
    {"OrderID": 10260, "CustomerID": "OTTIK", "OrderDate": "2024-01-19T00:00:00Z", "TotalAmount": 55.09, "Status": "Shipped"},
    {"OrderID": 10261, "CustomerID": "QUEDE", "OrderDate": "2024-01-19T00:00:00Z", "TotalAmount": 3.05, "Status": "Pending"},
    {"OrderID": 10262, "CustomerID": "RATTC", "OrderDate": "2024-01-22T00:00:00Z", "TotalAmount": 48.29, "Status": "Shipped"},
]


PRODUCTS: List[Dict[str, Any]] = [
    {"ProductID": 1, "ProductName": "Chai", "Category": "Beverages", "UnitPrice": 18.0, "UnitsInStock": 39},
    {"ProductID": 2, "ProductName": "Chang", "Category": "Beverages", "UnitPrice": 19.0, "UnitsInStock": 17},
    {"ProductID": 3, "ProductName": "Aniseed Syrup", "Category": "Condiments", "UnitPrice": 10.0, "UnitsInStock": 13},
    {"ProductID": 4, "ProductName": "Chef Anton's Cajun Seasoning", "Category": "Condiments", "UnitPrice": 22.0, "UnitsInStock": 53},
    {"ProductID": 5, "ProductName": "Grandma's Boysenberry Spread", "Category": "Condiments", "UnitPrice": 25.0, "UnitsInStock": 120},
    {"ProductID": 6, "ProductName": "Northwoods Cranberry Sauce", "Category": "Condiments", "UnitPrice": 40.0, "UnitsInStock": 6},
    {"ProductID": 7, "ProductName": "Tofu", "Category": "Produce", "UnitPrice": 23.25, "UnitsInStock": 35},
    {"ProductID": 8, "ProductName": "Konbu", "Category": "Seafood", "UnitPrice": 6.0, "UnitsInStock": 24},
    {"ProductID": 9, "ProductName": "Carnarvon Tigers", "Category": "Seafood", "UnitPrice": 62.5, "UnitsInStock": 42},
    {"ProductID": 10, "ProductName": "Sir Rodney's Marmalade", "Category": "Confections", "UnitPrice": 81.0, "UnitsInStock": 40},
]


ENTITIES: Dict[str, List[Dict[str, Any]]] = {
    "Customers": CUSTOMERS,
    "Orders": ORDERS,
    "Products": PRODUCTS,
}


@app.get("/$metadata")
async def get_metadata():
    return Response(content=CSDL, media_type="application/xml")


def _apply_filter(rows: List[Dict[str, Any]], expr: str) -> List[Dict[str, Any]]:
    """Very small $filter evaluator that supports:
       - field eq 'value'
       - field eq number
       - contains(field,'value')
    """
    if not expr:
        return rows
    expr = expr.strip()
    if expr.startswith("contains("):
        try:
            inner = expr[len("contains("):-1]
            field, _, value = inner.partition(",")
            field = field.strip()
            value = value.strip().strip("'")
            return [r for r in rows if value in str(r.get(field, ""))]
        except Exception:
            return rows
    if " eq " in expr:
        field, _, value = expr.partition(" eq ")
        field = field.strip()
        value = value.strip().strip("'")
        out = []
        for r in rows:
            if str(r.get(field, "")) == value:
                out.append(r)
        return out
    return rows


def _apply_select(rows: List[Dict[str, Any]], select: Optional[str]) -> List[Dict[str, Any]]:
    if not select:
        return rows
    fields = [f.strip() for f in select.split(",") if f.strip()]
    return [{k: r.get(k) for k in fields} for r in rows]


def _apply_orderby(rows: List[Dict[str, Any]], orderby: Optional[str]) -> List[Dict[str, Any]]:
    if not orderby:
        return rows
    parts = orderby.split()
    field = parts[0]
    reverse = len(parts) > 1 and parts[1].lower() == "desc"
    return sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=reverse)


def _apply_expand(rows: List[Dict[str, Any]], expand: Optional[str], entity_set: str) -> List[Dict[str, Any]]:
    if not expand:
        return rows
    expanded_names = [e.strip() for e in expand.split(",") if e.strip()]
    out = []
    for r in rows:
        new = dict(r)
        for nav in expanded_names:
            if entity_set == "Customers" and nav == "Orders":
                new["Orders"] = [o for o in ORDERS if o["CustomerID"] == r.get("CustomerID")]
            elif entity_set == "Orders" and nav == "Customer":
                cust = next((c for c in CUSTOMERS if c["CustomerID"] == r.get("CustomerID")), None)
                new["Customer"] = cust
            elif entity_set == "Orders" and nav == "Products":
                new["Products"] = PRODUCTS[:3]
        out.append(new)
    return out


@app.get("/{entity_set}")
async def list_entities(entity_set: str, request: Request):
    if entity_set not in ENTITIES:
        raise HTTPException(status_code=404, detail=f"Entity set '{entity_set}' not found")
    rows = list(ENTITIES[entity_set])
    q = parse_qs(request.url.query)
    if "filter" in q:
        rows = _apply_filter(rows, q["filter"][0])
    rows = _apply_orderby(rows, q.get("orderby", [None])[0])
    if "skip" in q:
        try:
            rows = rows[int(q["skip"][0]):]
        except Exception:
            pass
    if "top" in q:
        try:
            rows = rows[: int(q["top"][0])]
        except Exception:
            pass
    rows = _apply_expand(rows, q.get("expand", [None])[0], entity_set)
    rows = _apply_select(rows, q.get("select", [None])[0])
    payload: Dict[str, Any] = {"@odata.context": f"/$metadata#Sample.Northwind.{entity_set}", "value": rows}
    if q.get("count", ["false"])[0].lower() == "true":
        payload["@odata.count"] = len(rows)
    return JSONResponse(content=payload)


@app.get("/{entity_set}({entity_id})")
async def get_entity(entity_set: str, entity_id: str):
    if entity_set not in ENTITIES:
        raise HTTPException(status_code=404, detail=f"Entity set '{entity_set}' not found")
    for r in ENTITIES[entity_set]:
        for k, v in r.items():
            if str(v) == entity_id:
                return JSONResponse(content=r)
    raise HTTPException(status_code=404, detail="Not found")
