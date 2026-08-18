def read_config(path):
    config = {}
    with open(path) as f:
        for line in f:
            key, value = line.strip().split("=")
            config[key] = value.strip()
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