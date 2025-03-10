def standard_norm(X, mean, std):
    from sklearn.preprocessing import StandardScaler

    sc = StandardScaler(with_mean=mean, with_std=std)
    tX = sc.fit_transform(X)
    return tX


def split_dictionary(original_dict: dict, keys_to_remove: list):
    removed_dict = {
        key: original_dict[key] for key in keys_to_remove if key in original_dict
    }
    remaining_dict = {
        key: value for key, value in original_dict.items() if key not in keys_to_remove
    }

    return remaining_dict, removed_dict


def recursive_flatten_params(params, result_params: dict, nest_counter=0):
    # avoid infinite loops
    assert nest_counter <= 2, f"Nest depth overflow. [{nest_counter}]"
    nest_counter += 1

    for key, nested_param in params.items():
        if type(nested_param) is dict:
            recursive_flatten_params(nested_param, result_params, nest_counter)
        else:
            result_params[key] = nested_param


def param_check(params):
    for key in params:
        if (params[key] == "") or (params[key] == "None"):
            params[key] = None
    return params
