# checktags
Finds missing tags on EC2 instances and EBS volumes and generates a report on which tags are missing. Allows for multiple AWS accounts to be checked in a single session using IAM roles via STS, and saves state between sessions using DynamoDB.
