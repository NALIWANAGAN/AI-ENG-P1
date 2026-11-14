import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
import os
import logging
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Allow-list: the ONLY top-level statement types the LLM may run.
ALLOWED_TOP_LEVEL = (exp.Insert, exp.Update, exp.Select, exp.SetOperation)

# Second line of defence: none of these may appear ANYWHERE in the tree
# (e.g. hidden inside a CTE or a subquery).
FORBIDDEN_ANYWHERE = (
    exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.TruncateTable,
    exp.Copy, exp.Into, exp.Command,
)

class SQLValidatorResult(BaseModel):
    allowed: bool = False
    notice: str # human/LLM-readable; this goes back to the model when rejected
    normalized_sql: str | None = None

class ModelSQLValidator:
    
    def __init__(self, dialect: str = "postgres"):
        self.dialect = dialect or os.getenv("SQLGLOT_DB_DIALECT", "postgres")
        
    @staticmethod
    def _reject(notice: str) -> SQLValidatorResult:
        return SQLValidatorResult(allowed=False, notice=notice)
        
    def check_sql(self, raw_model_query: str | None = None) -> SQLValidatorResult:
        """Allow only single INSERT/UPDATE/SELECT statements. Reject everything else."""
        # empty input
        if raw_model_query is None or not raw_model_query.strip():
            return self._reject("Cannot have query without statements. Try at least 1 statement e.g: SELECT * FROM...")
            
        # parse (TokenError and ParseError both inherit from SqlglotError)
        try:
            parsed = sqlglot.parse(raw_model_query, read=self.dialect)
        except SqlglotError as exc:
            logger.warning("[WARNING] SQL rejected, could not parse: %s", exc)
            return self._reject(f"SQL syntax error: {exc}")
        
        # exactly one real statement (drop the None items left by ';' / comments)
        statements = [s for s in parsed if s is not None]
        logger.info("[INFO] Statement: %s", statements)
        if not statements:
            return self._reject("Query contains no SQL statement. Example: SELECT * FROM vendors;")
        if len(statements) > 1:
            return self._reject("Only one SQL statement per call is allowed.")
        
        node = statements[0]
        
        # allow-list on the TOP-LEVEL type
        if not isinstance(node, ALLOWED_TOP_LEVEL):
            return self._reject("Only INSERT, UPDATE and SELECT statements are allowed.")

        # deny anything dangerous nested inside an allowed statement
        bad = node.find(*FORBIDDEN_ANYWHERE)
        if bad is not None:
            return self._reject(f"{type(bad).__name__.upper()} operations are not allowed.")
        
        # apply the "no hidden INSERT/UPDATE" rule to everything that isn't itself an INSERT/UPDATE
        if not isinstance(node, (exp.Insert, exp.Update)) and node.find(exp.Insert, exp.Update):
            return self._reject("SELECT must not contain INSERT or UPDATE.")

        # every UPDATE must have a WHERE
        for update in node.find_all(exp.Update):
            if not update.args.get("where"):
                return self._reject("UPDATE must have a WHERE clause to avoid changing every row.")
            
        return SQLValidatorResult(
            allowed=True, 
            notice="This SQL query is allowed.",
            normalized_sql=node.sql(dialect=self.dialect)
        )

sql_validator = ModelSQLValidator()
