class DataProcessor:
    def __init__(self, data):
        self.data = data
        self.results = []

    def process(self):
        for record in self.data:
            # Accept either dicts with "value" or plain strings
            if isinstance(record, dict):
                raw = record.get("value", "")
            else:
                raw = record
            cleaned = raw.strip().lower().split(" ")[0] if raw else ""
            if cleaned:
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