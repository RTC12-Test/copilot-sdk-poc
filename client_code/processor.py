class DataProcessor:
    def __init__(self, data):
        self.data = data
        self.results = []

    def process(self):
        for record in self.data:
            # handle dict with "value" key or plain string
            if isinstance(record, dict):
                value = record.get("value", "")
            else:
                value = record
            parts = value.strip().lower().split()
            cleaned = parts[0] if parts else ""
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