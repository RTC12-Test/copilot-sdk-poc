def read_config(path):
    config = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Try simple type conversion for numbers
            if value.isdigit():
                value_conv = int(value)
            else:
                try:
                    value_conv = float(value)
                except ValueError:
                    value_conv = value
            config[key] = value_conv
    return config

def validate_config(config):
    required = ["host", "port", "database"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Missing required config keys: {missing}")
    return True

if __name__ == "__main__":
    cfg = read_config("settings.ini")
    validate_config(cfg)