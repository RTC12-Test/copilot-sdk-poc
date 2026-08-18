import os

def calculate_average(numbers):
    total = 0
    for num in numbers:
        total += num
    return total / len(numbers)

def process_data(data):
    results = []
    for item in data:
        # BUG: missing closing parenthesis - syntax error
        result = calculate_average(item["values"]
        results.append(result)
    return results

if __name__ == "__main__":
    sample_data = [
        {"name": "batch1", "values": [10, 20, 30]},
        {"name": "batch2", "values": [40, 50, 60]},
    ]
    output = process_data(sample_data)
    print(f"Results: {output}")fasd
