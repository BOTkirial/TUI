"""Abstraction fine au-dessus de psycopg (Postgres) et PyMySQL (MySQL/MariaDB).

Tous les appels sont bloquants : l'app Textual les exécute dans un worker de
type thread pour ne pas figer l'interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .connections import Connection

# table qualifiée : (schéma ou base, nom)
TableRef = tuple[str, str]


@dataclass(frozen=True)
class ColumnSource:
    """Colonne de table d'où provient une colonne du résultat."""

    table: TableRef
    column: str

    @property
    def label(self) -> str:
        return f"{self.table[0]}.{self.table[1]}.{self.column}"


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    rowcount: int
    message: str = ""
    # par colonne du résultat : colonne d'origine si elle est modifiable, sinon None
    sources: list[ColumnSource | None] = field(default_factory=list)
    # tables modifiables -> index (dans le résultat) des colonnes de leur clé primaire
    keys: dict[TableRef, list[int]] = field(default_factory=dict)
    # True si des lignes au-delà de `max_rows` ont été ignorées
    truncated: bool = False


@dataclass
class RowUpdate:
    """UPDATE d'une ligne : nouvelles valeurs (None = NULL) et clé primaire d'origine."""

    table: TableRef
    values: dict[str, str | None]
    key: dict[str, Any]


class Database:
    """Connexion vivante vers une base, indépendante du moteur."""

    def __init__(self, connection: Connection):
        self.meta = connection
        self._conn = None
        self._tunnel = None

    def connect(self) -> None:
        """Ouvre la connexion, via le tunnel SSH s'il est configuré."""
        host, port = self.meta.host, self.meta.port
        try:
            if self.meta.uses_ssh:
                from .tunnel import SshTunnel

                self._tunnel = SshTunnel(self.meta)
                host, port = self._tunnel.start()
            self._connect_db(host, port)
        except Exception:
            self.close()
            raise

    def _connect_db(self, host: str, port: int) -> None:
        if self.meta.driver == "postgres":
            import psycopg

            self._conn = psycopg.connect(
                host=host, port=port,
                dbname=self.meta.database, user=self.meta.user,
                password=self.meta.password, connect_timeout=10,
            )
        elif self.meta.driver in ("mysql", "mariadb"):
            import pymysql
            from pymysql.constants import CLIENT

            self._conn = pymysql.connect(
                host=host, port=port,
                database=self.meta.database or None, user=self.meta.user,
                password=self.meta.password, connect_timeout=10,
                # rowcount = lignes trouvées (comme Postgres), pas seulement changées :
                # sinon un UPDATE sans changement effectif ressemble à une ligne disparue
                client_flag=CLIENT.FOUND_ROWS,
            )
        else:
            raise ValueError(f"Pilote non supporté : {self.meta.driver}")

    def close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._conn = None
            if self._tunnel is not None:
                self._tunnel.close()
                self._tunnel = None

    def execute(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Exécute une requête et renvoie le résultat normalisé.

        `max_rows` : lignes lues au plus (le reste est ignoré, `truncated` = True).
        """
        if self._conn is None:
            raise RuntimeError("Non connecté")

        with self._conn.cursor() as cur:
            try:
                cur.execute(sql)
                rows = None
                if cur.description:  # requête qui renvoie des lignes
                    if max_rows is None:
                        rows = cur.fetchall()
                    else:
                        rows = cur.fetchmany(max_rows + 1)
                # validé dans tous les cas : un SELECT laissé en transaction garde
                # ses verrous (Postgres) ou fige l'instantané lu (MySQL, REPEATABLE
                # READ : changements des autres invisibles) ; un UPDATE … RETURNING
                # renvoie des lignes et serait perdu à la fermeture
                self._conn.commit()
            except Exception:
                # sans rollback, Postgres refuse toute requête suivante
                # (« current transaction is aborted »)
                self._conn.rollback()
                raise
            if rows is not None:
                rows = list(rows)
                truncated = max_rows is not None and len(rows) > max_rows
                if truncated:
                    rows = rows[:max_rows]
                columns = [desc[0] for desc in cur.description]
                result = QueryResult(
                    columns=columns, rows=rows, rowcount=len(rows), truncated=truncated
                )
                self._detect_editable(cur, result)
                return result
            # requête de modification (INSERT/UPDATE/DELETE/DDL)
            return QueryResult(
                columns=[], rows=[], rowcount=cur.rowcount,
                message=f"{cur.rowcount} ligne(s) affectée(s)",
            )

    @property
    def has_databases(self) -> bool:
        """MySQL/MariaDB : plusieurs bases par connexion, choisies par `USE`.

        (Postgres : une base par connexion ; sans nom, c'est celle de l'utilisateur.)
        """
        return self.meta.driver in ("mysql", "mariadb")

    def list_databases(self) -> list[str]:
        """Bases visibles par l'utilisateur (MySQL/MariaDB)."""
        return [str(row[0]) for row in self.execute("SHOW DATABASES").rows]

    def use_database(self, name: str) -> None:
        """Change la base courante (MySQL/MariaDB), équivalent de `USE`."""
        if self._conn is None:
            raise RuntimeError("Non connecté")
        self._conn.select_db(name)

    def list_tables(self) -> list[str]:
        """Liste les tables de la base courante."""
        if self.meta.driver == "postgres":
            # quote_ident : noms en majuscules/espaces directement utilisables en SQL
            sql = (
                "SELECT quote_ident(table_schema) || '.' || quote_ident(table_name) "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY 1"
            )
        else:
            sql = "SHOW TABLES"
        result = self.execute(sql)
        return [str(row[0]) for row in result.rows]

    def quote_table(self, name: str) -> str:
        """Nom de table tel que listé par `list_tables`, utilisable dans une requête."""
        # Postgres : déjà échappé par quote_ident dans list_tables
        if self.meta.driver == "postgres" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            return name
        return "`" + name.replace("`", "``") + "`"

    # --- édition des résultats --------------------------------------------
    def apply_updates(self, updates: list[RowUpdate]) -> None:
        """Applique les UPDATE dans une seule transaction : tout ou rien."""
        if self._conn is None:
            raise RuntimeError("Non connecté")

        with self._conn.cursor() as cur:
            try:
                for update in updates:
                    cur.execute(*self._update_sql(update))
                    # 0 ligne = supprimée/modifiée entre-temps ; on refuse d'écrire à l'aveugle
                    if cur.rowcount != 1:
                        raise RuntimeError(
                            f"{update.table[0]}.{update.table[1]} {update.key} : "
                            f"{cur.rowcount} ligne(s) touchée(s) au lieu d'une"
                        )
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _quote(self, name: str) -> str:
        """Identifiant échappé pour une requête paramétrée."""
        quote = '"' if self.meta.driver == "postgres" else "`"
        # % doublé : psycopg et PyMySQL l'interprètent comme marqueur de paramètre
        return quote + name.replace(quote, quote * 2).replace("%", "%%") + quote

    def _update_sql(self, update: RowUpdate) -> tuple[str, list[Any]]:
        table = f"{self._quote(update.table[0])}.{self._quote(update.table[1])}"
        assignments = ", ".join(f"{self._quote(col)} = %s" for col in update.values)
        condition = " AND ".join(f"{self._quote(col)} = %s" for col in update.key)
        params = [*update.values.values(), *update.key.values()]
        return f"UPDATE {table} SET {assignments} WHERE {condition}", params

    def _detect_editable(self, cur, result: QueryResult) -> None:
        """Renseigne `sources`/`keys` : quelles colonnes du résultat sont modifiables.

        Une colonne l'est si elle provient directement d'une table dont toute la
        clé primaire figure aussi dans le résultat (sinon impossible de cibler la
        ligne). Échec de détection = résultat en lecture seule, sans erreur.
        """
        result.sources = [None] * len(result.columns)
        try:
            if self.meta.driver == "postgres":
                origins, primary_keys = self._origins_postgres(cur)
            else:
                origins, primary_keys = self._origins_mysql(cur)
        except Exception:
            return
        finally:
            # lectures de métadonnées seules : on referme la transaction ouverte
            self._conn.rollback()

        for table, pk_columns in primary_keys.items():
            key_indexes = [
                next((i for i, o in enumerate(origins) if o == (table, col)), None)
                for col in pk_columns
            ]
            if pk_columns and None not in key_indexes:
                result.keys[table] = key_indexes
        for i, origin in enumerate(origins):
            if origin is not None and origin[0] in result.keys:
                result.sources[i] = ColumnSource(*origin)

    def _origins_postgres(self, cur):
        """Origine de chaque colonne via les métadonnées du protocole Postgres."""
        res = cur.pgresult
        # (oid de la table, n° de colonne), 0 pour une expression calculée
        raw = [(res.ftable(i), res.ftablecol(i)) for i in range(res.nfields)]
        oids = sorted({oid for oid, _ in raw if oid})
        if not oids:
            return [None] * len(raw), {}

        with self._conn.cursor() as meta:
            meta.execute(
                "SELECT c.oid::int8, n.nspname, c.relname, a.attnum, a.attname, "
                "       COALESCE(a.attnum = ANY(i.indkey), false) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary "
                "WHERE c.oid::int8 = ANY(%s)",
                (oids,),
            )
            columns: dict[tuple[int, int], tuple[TableRef, str]] = {}
            primary_keys: dict[TableRef, list[str]] = {}
            for oid, schema, table, attnum, attname, is_pk in meta.fetchall():
                columns[(oid, attnum)] = ((schema, table), attname)
                if is_pk:
                    primary_keys.setdefault((schema, table), []).append(attname)
        return [columns.get(item) for item in raw], primary_keys

    def _origins_mysql(self, cur):
        """Origine de chaque colonne via les descripteurs de champ MySQL."""
        origins: list[tuple[TableRef, str] | None] = []
        # attribut privé de PyMySQL : seul accès à org_table / org_name (alias résolus)
        for f in cur._result.fields:
            db = f.db.decode() if isinstance(f.db, bytes) else f.db
            origins.append(((db, f.org_table), f.org_name) if db and f.org_table else None)

        primary_keys: dict[TableRef, list[str]] = {}
        with self._conn.cursor() as meta:
            for table in {o[0] for o in origins if o}:
                meta.execute(
                    "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY' "
                    "ORDER BY ORDINAL_POSITION",
                    table,
                )
                primary_keys[table] = [row[0] for row in meta.fetchall()]
        return origins, primary_keys
