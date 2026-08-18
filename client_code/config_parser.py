def read_config(path):
    config = {}
    with open(path) as f:
        for line in f:
            key, value = line.strip().split("=")
            # BUG: value not stripped and no type conversion
            config[key] = value
    return config

def validate_config(config):
    required = ["host", "port", "database"]
    missing = [key for key in required if key not in config]
    if missing:
        # BUG: f-string missing closing brace
        raise ValueError(f"Missing required config keys: {missing
    return True

if __name__ == "__main__":
    cfg = read_config("settings.ini")
    validate_config(cfg)asd
