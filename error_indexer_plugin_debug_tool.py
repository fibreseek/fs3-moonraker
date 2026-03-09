
import pexpect
import sys
import time
import re

class MoonrakerDebugger:
    def __init__(self, host, user, password):
        self.host = host
        self.user = user
        self.password = password
        self.config_path = "~/printer_data/config/moonraker.conf"
        
    def log(self, msg):
        print(f"[DEBUG] {msg}")

    def run_cmd(self, cmd, timeout=30, check_exit=True):
        ssh_cmd = f"ssh -o StrictHostKeyChecking=no {self.user}@{self.host} '{cmd}'"
        # self.log(f"Running: {ssh_cmd}")
        child = pexpect.spawn(ssh_cmd)
        try:
            index = child.expect(['password:', pexpect.EOF], timeout=timeout)
            if index == 0:
                child.sendline(self.password)
                child.expect(pexpect.EOF, timeout=timeout)
            
            output = child.before.decode() if child.before else ""
            child.close()
            
            if check_exit and child.exitstatus != 0:
                self.log(f"Command failed: {cmd}")
                self.log(f"Output: {output}")
                return False, output
            return True, output
        except Exception as e:
            self.log(f"Exception: {e}")
            return False, str(e)

    def scp_file(self, local, remote):
        cmd = f"scp -o StrictHostKeyChecking=no {local} {self.user}@{self.host}:{remote}"
        child = pexpect.spawn(cmd)
        try:
            index = child.expect(['password:', pexpect.EOF], timeout=30)
            if index == 0:
                child.sendline(self.password)
                child.expect(pexpect.EOF)
            child.close()
            return child.exitstatus == 0
        except Exception as e:
            self.log(f"SCP Error: {e}")
            return False

    def deploy(self):
        self.log("Step 1: Deploying files...")
        # Deploy plugin
        if not self.scp_file("moonraker/components/error_indexer.py", "~/moonraker/moonraker/components/"):
            return False
        # Deploy xlsx
        if not self.scp_file("error_info.xlsx", "~/moonraker/"):
            return False
        self.log("Files deployed successfully.")
        return True

    def configure(self):
        self.log("Step 2: Checking and enforcing configuration...")
        
        # Always check current content first
        success, output = self.run_cmd(f"cat {self.config_path}", check_exit=False)
        if not success:
            self.log(f"Failed to read config: {output}")
            return False

        if "[error_indexer]" in output:
            self.log("Configuration [error_indexer] already exists.")
            # Verify if it's correct/complete? Assuming yes for now.
            return True
        
        self.log("Appending configuration...")
        # Use echo to append safely, simpler than python -c with quotes hell
        config_content = "\\n[error_indexer]\\nfile_path: /home/anisoprint/moonraker/error_info.xlsx\\nlanguage: en\\n"
        cmd = f"echo -e \"{config_content}\" >> {self.config_path}"
        success, out = self.run_cmd(cmd)
        if success:
            self.log("Configuration appended.")
            # Verify again
            s, o = self.run_cmd(f"grep '\\[error_indexer\\]' {self.config_path}", check_exit=False)
            if s: self.log("Verified: Config appended successfully.")
            else: self.log("Error: Config appended but not found!")
        else:
            self.log(f"Failed to append config: {out}")
            
        return success

    def restart(self):
        self.log("Step 3: Restarting Moonraker...")
        cmd = f"echo {self.password} | sudo -S systemctl restart moonraker"
        success, out = self.run_cmd(cmd, timeout=60)
        if success:
            self.log("Service restarted.")
            # Wait a bit for startup
            time.sleep(5)
        return success

    def analyze_logs(self):
        self.log("Step 4: Analyzing logs and config...")
        
        # Check config content
        self.log("--- Current Moonraker Config ---")
        success, config_out = self.run_cmd(f"cat {self.config_path}")
        if success:
             # Just show the error_indexer part
             if "[error_indexer]" in config_out:
                 lines = config_out.split('\n')
                 for i, line in enumerate(lines):
                     if "[error_indexer]" in line:
                         self.log("Found config section:")
                         for j in range(i, min(i+5, len(lines))):
                             self.log(f"  {lines[j]}")
             else:
                 self.log("❌ [error_indexer] section NOT found in config!")

        # Get more log lines
        success, output = self.run_cmd("tail -n 500 ~/printer_data/logs/moonraker.log")
        if not success:
            return False

        # Analysis
        self.log("--- Log Analysis Report ---")
        
        # Check for plugin loading
        # Moonraker usually logs "Component 'xxx' loaded" or "Performing Component Post Init"
        # Let's search for our specific component name
        
        found_init = False
        for line in output.split('\n'):
            if "error_indexer" in line:
                self.log(f"Found log entry: {line.strip()}")
                found_init = True
        
        if not found_init:
            self.log("⚠️ No log entries found containing 'error_indexer'.")
            self.log("Possible reasons: Plugin file location wrong? Config ignored?")
        
        # Check for our specific log messages
        if "从 xlsx 成功加载" in output or "成功加载" in output:
            match = re.search(r"成功加载 (\d+) 条错误记录", output)
            count = match.group(1) if match else "?"
            self.log(f"✅ Database loaded with {count} entries.")
        if "未找到 xlsx 文件" in output:
            self.log("❌ xlsx file missing!")

        # Print recent errors
        self.log("--- Recent Errors in Log ---")
        lines = output.split('\n')
        for line in lines[-20:]: # Show last 20 lines anyway
            if "Error" in line or "Exception" in line or "Traceback" in line:
                print(f"   {line.strip()}")
        
        return True

    def run(self):
        if not self.deploy(): return
        if not self.configure(): return
        if not self.restart(): return
        self.analyze_logs()

if __name__ == "__main__":
    debugger = MoonrakerDebugger("192.168.110.10", "anisoprint", "anaisuo")
    debugger.run()
