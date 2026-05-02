# Terminology and Naming Map

Canonical reference for operator naming across Python APIs and GraphQL
wire fields. Refer to `docs/SPEC.md` for normative behavior semantics.

| Enum member            | Python API surface             | GraphQL wire name              | Alias(es) | Backend notes                                                      |
| ---------------------- | ------------------------------ | ------------------------------ | --------- | ------------------------------------------------------------------ |
| `COUNT`                | `count`                        | `count`                        | —         | SQLite + PostgreSQL                                                |
| `COUNT_DISTINCT`       | `count_distinct`               | `countDistinct(field: ...)`    | —         | SQLite + PostgreSQL                                                |
| `COUNT_DISTINCT_TUPLE` | `count_distinct(fields=[...])` | `countDistinct(fields: [...])` | —         | PostgreSQL native; SQLite emulated via NULL-coalesced concatenation |
| `SUM`                  | `sum`                          | `sum` nested object            | —         | SQLite + PostgreSQL                                                |
| `AVG`                  | `avg`                          | `avg` nested object            | —         | SQLite + PostgreSQL                                                |
| `MIN`                  | `min`                          | `min` nested object            | —         | SQLite + PostgreSQL                                                |
| `MAX`                  | `max`                          | `max` nested object            | —         | SQLite + PostgreSQL                                                |
| `STDDEV`               | `stddev`                       | `stddev` nested object         | —         | PostgreSQL only                                                    |
| `VARIANCE`             | `variance`                     | `variance` nested object       | —         | PostgreSQL only                                                    |
| `STDDEV_POP`           | `stddev_pop`                   | `stddevPop` nested object      | —         | PostgreSQL only                                                    |
| `VAR_POP`              | `var_pop`                      | `varPop` nested object         | —         | PostgreSQL only                                                    |
| `BOOL_AND`             | `bool_and`                     | `boolAnd`                      | `every`   | SQLite + PostgreSQL                                                |
| `BOOL_OR`              | `bool_or`                      | `boolOr`                       | `some`    | SQLite + PostgreSQL                                                |
| `ARRAY_AGG`            | `array_agg`                    | `arrayAgg` nested object       | —         | PostgreSQL only                                                    |
| `STRING_AGG`           | `string_agg`                   | `stringAgg` nested object      | —         | PostgreSQL only                                                    |
| `PERCENTILE_CONT`      | `percentile_cont`              | `percentileCont(field, fraction)` | —      | PostgreSQL only                                                    |
| `PERCENTILE_DISC`      | `percentile_disc`              | `percentileDisc(field, fraction)` | —      | PostgreSQL only                                                    |
| `MODE`                 | `mode`                         | `mode` nested object           | —         | PostgreSQL only                                                    |

## Grouping vocabulary

- **group_by (Python)** ↔ **groupBy (GraphQL)**
- **having (Python)** ↔ **having (GraphQL)**
- **order_by (Python)** ↔ **orderBy (GraphQL)**
- **count_distinct (Python)** ↔ **countDistinct (GraphQL)**
