"""Field semantics supplied by the PostgreSQL dictionaries to LLM calls."""

def field_context(store):
    labels = store.field_labels()
    values = store.field_value_labels()
    return {key: {'label': row['label'], 'description': row['description'],
                  'coded_values': values.get(key, {})}
            for key, row in labels.items()}
