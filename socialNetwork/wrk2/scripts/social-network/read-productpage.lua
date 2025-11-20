local socket = require("socket")
local time = socket.gettime()*1000
math.randomseed(time)
math.random(); math.random(); math.random()

-- load env vars
local max_user_index = tonumber(os.getenv("max_user_index")) or 962

request = function()
  local user_id = tostring(math.random(0, max_user_index - 1))
  local start = tostring(math.random(0, 100))
  local stop = tostring(start + 10)

  local args = "user_id=" .. user_id .. "&start=" .. start .. "&stop=" .. stop
  local method = "GET"
  local headers = {}
  -- headers["Content-Type"] = "application/x-www-form-urlencoded"
  headers["Content-Type"] = "application/json"
  -- local path = "http://localhost:8080/wrk2-api/home-timeline/read?" .. args
  local path = "/productpage"
  return wrk.format(method, path, headers, nil)

end

-- remove this to disable response logging
-- response = function(status, headers, body)
--     -- Log the status code
--     io.stderr:write("Status: " .. status .. "\n")

--     -- Log specific headers (e.g., Content-Type)
--     if headers["Content-Type"] then
--         io.stderr:write("Content-Type: " .. headers["Content-Type"] .. "\n")
--     end

--     -- Log the response body (be cautious with large bodies)
--     -- You might want to truncate or only log if specific conditions are met
--     io.stderr:write("Body: " .. body .. "\n")
-- end

-- response = function(status, headers, body)
--   if status ~= 200 then
--     io.write("Status: ".. status .."\n")
--     io.write("Body:\n")
--     io.write(body .. "\n")
--   end
-- end