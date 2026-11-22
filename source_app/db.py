"""Shared SQL Server connection helper (Windows authentication)."""
import os

import pyodbc
from dotenv import load_dotenv

load_dotenv()

SERVER = os.getenv("SQL_SERVER", r"localhost\MSSQLSERVER02")
DATABASE = os.getenv("SQL_DATABASE", "ShopDB")
DRIVER = os.getenv("SQL_DRIVER", "ODBC Driver 18 for SQL Server")


def get_connection(database: str = DATABASE, autocommit: bool = False) -> pyodbc.Connection:
    conn_str = (
        f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={database};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, autocommit=autocommit)
