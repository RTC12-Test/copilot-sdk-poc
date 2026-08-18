def calculate_discount(price, discount_percent):
    discount = price * discount_percent / 100
    final_price = price - discount
    return final_price

def apply_bulk_discount(items):
    results = []
    for item in items:
        result = calculate_discount(item["price"], item["discount"])
        results.append(result)
    return results

if __name__ == "__main__":
    data = [
        {"name": "Widget", "price": 100, "discount": 10},
        {"name": "Gadget", "price": 200, "discount": 20},
    ]
    print(apply_bulk_discount(data))