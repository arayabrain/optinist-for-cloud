from studio.app.optinist.wrappers.custom.custom_node import my_function

custom_wrapper_dict = {
    "custom_node": {
        "my_function": {
            "function": my_function,
            "conda_name": "my_custom_env",
        },
    }
}
