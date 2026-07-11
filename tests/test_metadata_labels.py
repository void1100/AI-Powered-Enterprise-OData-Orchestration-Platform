import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.odata_client import ODataClient


class MetadataLabelTests(unittest.TestCase):
    def test_sap_labels_are_extracted_from_metadata(self):
        xml = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" xmlns:sap="http://www.sap.com/Protocols/SAPData">
  <edmx:DataServices>
    <Schema Namespace="Demo" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="ProductsType" sap:label="Product">
        <Key><PropertyRef Name="ProductID"/></Key>
        <Property Name="ProductID" Type="Edm.Int32" Nullable="false" sap:label="Product ID"/>
        <Property Name="UnitPrice" Type="Edm.Decimal" sap:label="Unit Price"/>
      </EntityType>
      <EntityContainer Name="DemoService">
        <EntitySet Name="Products" EntityType="Demo.ProductsType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""
        metadata = ODataClient("http://example.test")._parse_metadata(xml)

        entity = metadata["entity_types"][0]
        labels = {p["name"]: p["label"] for p in entity["properties"]}

        self.assertEqual(entity["label"], "Product")
        self.assertEqual(labels["ProductID"], "Product ID")
        self.assertEqual(labels["UnitPrice"], "Unit Price")


if __name__ == "__main__":
    unittest.main()
