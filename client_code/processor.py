class DataProcessor:
    def __init__(self, data):
        self.data = data
        self.results = []

    def process(self):
        for record in self.data:
            # Normalize input whether it's a string or dict with "value"
            if isinstance(record, str):
                source = record
            elif isinstance(record, dict) and "value" in record:
                source = record["value"] or ""
            else:
                source = str(record)
            cleaned = source.strip().lower().split()[0] if source.strip() else ""
            self.results.append(cleaned)

    def get_summary(self):
        return {
            "total": len(self.results),
            "unique": len(set(self.results)),
        }

if __name__ == "__main__":
    processor = DataProcessor(["  Hello ", " World ", " hello "])
    processor.process()
    print(processor.get_summary())