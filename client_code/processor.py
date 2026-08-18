class DataProcessor:
    def __init__(self, data):
        self.data = data
        self.results = []

    def process(self):
        for record in self.data:
            # BUG: unmatched bracket
            cleaned = record["value"].strip().lower().split(" ")[0]
            self.results.append(cleaned

    def get_summary(self):
        return {
            "total": len(self.results),
            "unique": len(set(self.results)),
        }

if __name__ == "__main__":
    processor = DataProcessor(["  Hello ", " World ", " hello "])
    processor.process()
    print(processor.get_summary()asjdaksjd((())(
