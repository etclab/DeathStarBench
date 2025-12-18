from pathlib import Path
import pprint
import os
import statistics

def parse_register(result, line):
    parts = line.split(',')
    # REGISTER,58885,15640|6255|65403,1765304067022145
    assert len(parts) == 4, f"{line} doesn't have four parts"
    
    user_id = parts[1]
    users_before_me = parts[2]
    register_time = parts[3]
    
    data = {}
    data['register_time'] = register_time
    
    users_before_me = users_before_me.strip()
    ubm = [user for user in users_before_me.split('|') if user.strip() != ""]
    data['users_before_me'] = ubm
    
    result[user_id] = data
    
def parse_ready(result, line):
    parts = line.split(',')
    # READY,65403,1765304079825262
    assert len(parts) == 3, f"{line} doesn't have three parts"
    
    user_id = parts[1]
    ready_time = parts[2]
    
    assert result.get(user_id) != None, f"{user_id} became ready before registration"
    
    data = result[user_id]
    data['ready_time'] = ready_time

def parse_opening_ack(result, line):
    parts = line.split(',')
    # ACK_OPENING,58885,58885,1765304082139756
    assert len(parts) == 4, f"{line} doesn't have four parts"
    
    other_user_id = parts[1]
    user_id = parts[2]
    opening_ack_time = parts[3]
    
    # look at the opening ack time for users before me
    assert result.get(user_id) != None, f"{user_id} became ready before registration"

    data = result[user_id]
    # keys are ids in users_before_me, values are times when they sent the acknowledgement
    if "user_ack_time" not in data:
        data["user_ack_time"] = {}
        
    users_before_me = data["users_before_me"]
    
    if other_user_id in users_before_me:
        data["user_ack_time"][other_user_id] = opening_ack_time
    
def compute_timings(result):
    # time taken for a pod to become ready
    ready_times = {}
    for user_id in result:
        data = result[user_id]
        
        self_ready_time = int(data["ready_time"]) - int(data["register_time"])
        
        times = {}
        times["self_ready_time"] = self_ready_time
        
        user_ack_time = data["user_ack_time"]
        max_time = -1
        max_key = None
        for key, value in user_ack_time.items():
            if int(value) > max_time:
                max_time = int(value)
                max_key = key
            
        if max_time != -1:
            assert int(user_ack_time[max_key]) > int(data["register_time"]), \
                f"Max user acknowledge time should be greater than register_time"
            times["extended_ready_time"] = int(user_ack_time[max_key]) - int(data["register_time"])
        else:
            # case for the first user and there's no users_before_me array
            times["extended_ready_time"] = int(data["ready_time"]) - int(data["register_time"])
            
        times["max_ready_time"] = max(times["extended_ready_time"], times["self_ready_time"])
        times["min_ready_time"] = min(times["extended_ready_time"], times["self_ready_time"])
        times["diff"] = times["max_ready_time"] - times["min_ready_time"]
        times["prev_users_count"] = len(user_ack_time)
        
        ready_times[user_id] = times
        
    return ready_times

# all timings are in microseconds 
def parse_log(file):

    REGISTER = "REGISTER"
    READY = "READY"
    ACK_OPENING = "ACK_OPENING"

    # write about the format for result
    result = {}

    with open(file, "r") as file:
        for line in file:
            line = line.strip()
            
            if line.startswith(REGISTER):
                parse_register(result, line)
            
            if line.startswith(READY):
                parse_ready(result, line)
                
            if line.startswith(ACK_OPENING):
                parse_opening_ack(result, line)
            
    # pprint.pprint(result, indent=2)
    
    ready_times = compute_timings(result)
    # pprint.pprint(ready_times, indent=2)
    
    for key, value in ready_times.items():
        time_us = value["max_ready_time"]
        time_ms = time_us/1000
        time_s = time_ms/1000
        # print(f"{key} took {time_s} s | {time_ms} ms | {time_us} us")
    
    return ready_times
    
if __name__ == "__main__":
    
    folders = ["pods-5", "pods-10", "pods-25", "pods-50"]
    current_dir = Path.cwd()
    
    for folder in folders:
        dir_path = os.path.join(current_dir, folder)
        files = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]
        
        all_file_path = os.path.join(dir_path, "all.csv")
        
        min_ready_times = []
        max_ready_times = []
        
        with open(all_file_path, "w") as f:
            f.write(f"{'min_ready_time':<20} {f'max_ready_time':<20}\n")
            
            for file in files:
                if file.startswith("all"): continue
                
                file_path = os.path.join(dir_path, file)
                
                timings = parse_log(file_path)
                
                last_one = None
                max_users = -1
                for key, value in timings.items():
                    if value["prev_users_count"] > max_users:
                        max_users = value["prev_users_count"]
                        last_one = timings[key] 
                
                assert last_one != None, f"last_one should not be None"
                
                min_ready_times.append(last_one['min_ready_time'])
                max_ready_times.append(last_one['max_ready_time'])

                f.write(f"{last_one['min_ready_time']:<20} {last_one['max_ready_time']:<20}\n")
                
        sorted_file_path = os.path.join(dir_path, "all-sorted.csv")
        
        dual = []
        for i in range(len(min_ready_times)):
            dual.append((min_ready_times[i], max_ready_times[i]))
        
        # sort by max_ready_time
        dual.sort(key=lambda x: x[1])
        
        with open(sorted_file_path, "w") as f:
            f.write(f"{'min_ready_time':<20} {f'max_ready_time':<20}\n")
            
            for val in dual:
                f.write(f"{val[0]:<20} {val[1]:<20}\n")

        sorted_min_path = os.path.join(dir_path, "all-sorted-min.csv")
        sorted_max_path = os.path.join(dir_path, "all-sorted-max.csv")
        with open(sorted_min_path, "w") as f:
            min_ready_times.sort()
            for val in min_ready_times:
                f.write(f"{val}\n")
                
        with open(sorted_max_path, "w") as f:
            max_ready_times.sort()
            for val in max_ready_times:
                f.write(f"{val}\n")
                
        summary_file_path = os.path.join(dir_path, "all-summary.csv")
        with open(summary_file_path, "w") as f:
            f.write(f"#{'metric':<20} {'min_ready_time':<20} {f'max_ready_time':<20}\n")
            f.write(f"#{'':<20} {'mean':<10} {f'std':<10} {'mean':<10} {f'std':<10}\n")
            f.write(f"{folder:<20} {statistics.mean(min_ready_times):<10} {statistics.stdev(min_ready_times):10} \
                {statistics.mean(max_ready_times):<10} {statistics.stdev(max_ready_times):10}")
            # f.write(f"{'min_ready_time':<20} {statistics.mean(min_ready_times):<20} {statistics.stdev(min_ready_times):<20}\n")
            # f.write(f"{'max_ready_time':<20} {statistics.mean(max_ready_times):<20} {statistics.stdev(max_ready_times):<20}\n")