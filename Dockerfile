# Use the official lightweight Python 3.11 image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the dependencies file and install the dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all remaining bot code into the container
COPY . .

# Command executed when the container starts
CMD ["python", "bot.py"]